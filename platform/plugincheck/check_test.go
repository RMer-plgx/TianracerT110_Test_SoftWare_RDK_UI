package plugincheck

import (
	"archive/zip"
	"bytes"
	"io/fs"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const fixture = "../product-plugins/sim-base"

func zipFixture(t *testing.T, extraName string, extraMode fs.FileMode, extraData []byte, deflate bool) string {
	t.Helper()
	dest := filepath.Join(t.TempDir(), "plugin.zip")
	out, err := os.Create(dest)
	if err != nil {
		t.Fatal(err)
	}
	archive := zip.NewWriter(out)
	if err := fs.WalkDir(os.DirFS(fixture), ".", func(name string, entry fs.DirEntry, err error) error {
		if err != nil || entry.IsDir() {
			return err
		}
		b, err := os.ReadFile(filepath.Join(fixture, name))
		if err != nil {
			return err
		}
		w, err := archive.CreateHeader(&zip.FileHeader{Name: name, Method: zip.Store})
		if err != nil {
			return err
		}
		_, err = w.Write(b)
		return err
	}); err != nil {
		t.Fatal(err)
	}
	if extraName != "" {
		h := &zip.FileHeader{Name: extraName, Method: zip.Store}
		h.SetMode(extraMode)
		if deflate {
			h.Method = zip.Deflate
		}
		w, err := archive.CreateHeader(h)
		if err != nil {
			t.Fatal(err)
		}
		if _, err := w.Write(extraData); err != nil {
			t.Fatal(err)
		}
	}
	if err := archive.Close(); err != nil {
		t.Fatal(err)
	}
	if err := out.Close(); err != nil {
		t.Fatal(err)
	}
	return dest
}

func copyFixture(t *testing.T) string {
	t.Helper()
	dest := t.TempDir()
	if err := os.CopyFS(dest, os.DirFS(fixture)); err != nil {
		t.Fatal(err)
	}
	return dest
}

func change(t *testing.T, dir, name, old, replacement string) {
	t.Helper()
	p := filepath.Join(dir, name)
	data, err := os.ReadFile(p)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(data), old) {
		t.Fatalf("missing mutation target %q", old)
	}
	if err := os.WriteFile(p, []byte(strings.Replace(string(data), old, replacement, 1)), 0644); err != nil {
		t.Fatal(err)
	}
}

func redigest(t *testing.T, dir string) {
	t.Helper()
	var b bytes.Buffer
	if err := WriteIntegrity(dir, &b); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "integrity.json"), b.Bytes(), 0644); err != nil {
		t.Fatal(err)
	}
}

func TestDirectoryAndZipAgree(t *testing.T) {
	base, err := Check(fixture, "darwin/arm64")
	if err != nil {
		t.Fatal(err)
	}
	zipped, err := Check(zipFixture(t, "", 0, nil, false), "darwin/arm64")
	if err != nil {
		t.Fatal(err)
	}
	if base != zipped || base.Mode != "development" {
		t.Fatalf("different results: %v %v", base, zipped)
	}
	serial, err := Check("../product-plugins/sim-serial", "linux/amd64")
	if err != nil || serial.ID == base.ID {
		t.Fatalf("serial: %v %v", serial, err)
	}
}

func TestBadPackages(t *testing.T) {
	for _, test := range []struct {
		name, file, old, change, problem string
		rehash                           bool
	}{
		{"digest mismatch", "main.ts", "type StepInput", "type Changed", "integrity mismatch", false},
		{"unknown api", "plugin.json", `"1.0"`, `"2.0"`, "unsupported plugin API", true},
		{"missing adapter", "plugin.json", `"sim.base"`, `"missing"`, "missing capability", true},
		{"permission outside capability", "plugin.json", "sim.base.read", "sim.base.motion", "unauthorized capability", true},
		{"wrong platform", "plugin.json", "darwin/arm64", "darwin/amd64", "platform not declared", true},
		{"missing export", "main.ts", "function execute", "function unrelated", "missing required export execute", true},
		{"missing criterion", "products/sim-base.json", `"model"`, `"missing"`, "missing criterion", true},
		{"manifest shape", "plugin.json", `"schemaVersion": 1`, `"schemaVersion": "1"`, "Manifest", true},
		{"external import", "main.ts", "type StepInput", "import 'https://example.com/x'; type StepInput", "external module imports", true},
	} {
		t.Run(test.name, func(t *testing.T) {
			dir := copyFixture(t)
			change(t, dir, test.file, test.old, test.change)
			if test.rehash {
				redigest(t, dir)
			}
			_, err := Check(dir, "darwin/arm64")
			if err == nil || !strings.Contains(err.Error(), test.problem) {
				t.Fatalf("expected %q, got %v", test.problem, err)
			}
		})
	}
}

func TestBadZipPathsAndLimits(t *testing.T) {
	for _, test := range []struct {
		path, problem string
		mode          fs.FileMode
		data          []byte
		deflate       bool
	}{
		{"../outside", "unsafe", 0644, nil, false},
		{"/absolute", "unsafe", 0644, nil, false},
		{"PLUGIN.JSON", "path conflict", 0644, nil, false},
		{"plugin.json", "duplicate", 0644, nil, false},
		{"link", "non-regular", fs.ModeSymlink | 0777, []byte("main.ts"), false},
		{"COM1.txt", "unsafe", 0644, nil, false},
		{"expanded.bin", "ratio limit", 0644, make([]byte, 100000), true},
		{"too-big.bin", "size limits", 0644, make([]byte, MaxFileBytes+1), false},
		{"unlisted.txt", "unlisted file", 0644, nil, false},
	} {
		t.Run(test.path, func(t *testing.T) {
			z := zipFixture(t, test.path, test.mode, test.data, test.deflate)
			_, err := Check(z, "darwin/arm64")
			if err == nil || !strings.Contains(err.Error(), test.problem) {
				t.Fatalf("expected %q, got %v", test.problem, err)
			}
		})
	}
}

func TestCheckDoesNotEvaluateModule(t *testing.T) {
	dir := copyFixture(t)
	change(t, dir, "main.ts", "type StepInput", "throw new Error('do not evaluate'); type StepInput")
	redigest(t, dir)
	if _, err := Check(dir, "darwin/arm64"); err != nil {
		t.Fatal(err)
	}
}
