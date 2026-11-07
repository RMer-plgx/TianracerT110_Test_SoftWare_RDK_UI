// Package plugincheck checks bounded directory and ZIP fixtures without
// evaluating JavaScript, extracting ZIPs, installing packages or opening devices.
package plugincheck

import (
	"archive/zip"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path"
	"regexp"
	"strings"
)

const (
	MaxEntries    = 128
	MaxFileBytes  = 1 << 20
	MaxTotalBytes = 8 << 20
	MaxZipBytes   = 16 << 20
	MaxRatio      = 100
	MaxPathBytes  = 240
)

var component = regexp.MustCompile(`^[A-Za-z0-9_.-]+$`)
var windowsDevice = regexp.MustCompile(`^(CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])$`)

func safePath(name string) bool {
	if len(name) > MaxPathBytes || name == "." || !fs.ValidPath(name) {
		return false
	}
	for segment := range strings.SplitSeq(name, "/") {
		base, _, _ := strings.Cut(segment, ".")
		if !component.MatchString(segment) || strings.HasSuffix(segment, ".") ||
			windowsDevice.MatchString(strings.ToUpper(base)) {
			return false
		}
	}
	return true
}

type treeEntry struct {
	name string
	dir  bool
}

type snapshot struct {
	files     map[string][]byte
	tree      map[string]treeEntry
	explicit  map[string]bool
	remaining int64
}

func newSnapshot() *snapshot {
	return &snapshot{map[string][]byte{}, map[string]treeEntry{}, map[string]bool{}, MaxTotalBytes}
}

func (s *snapshot) register(name string, dir bool) error {
	if !safePath(name) {
		return fmt.Errorf("unsafe package path %q", name)
	}
	if len(s.explicit) >= MaxEntries {
		return errors.New("package entry count exceeds limit")
	}
	if s.explicit[name] {
		return fmt.Errorf("duplicate package path %q", name)
	}
	s.explicit[name] = true
	for entry := (treeEntry{name, dir}); entry.name != "."; entry = (treeEntry{path.Dir(entry.name), true}) {
		key := strings.ToLower(entry.name)
		if old, ok := s.tree[key]; ok && (entry != old || !entry.dir) {
			return fmt.Errorf("package path conflict: %q and %q", entry.name, old.name)
		}
		s.tree[key] = entry
	}
	return nil
}

func (s *snapshot) read(name string, size uint64, open func() (io.ReadCloser, error)) error {
	if size > MaxFileBytes || size > uint64(s.remaining) {
		return fmt.Errorf("%q exceeds package size limits", name)
	}
	r, err := open()
	if err != nil {
		return err
	}
	defer r.Close()
	data, err := io.ReadAll(io.LimitReader(r, int64(size)+1))
	if err != nil {
		return fmt.Errorf("%q: %w", name, err)
	}
	if uint64(len(data)) != size {
		return fmt.Errorf("%q: unexpected file size", name)
	}
	s.remaining -= int64(size)
	s.files[name] = data
	return nil
}

func scanDir(rootName string, s *snapshot) error {
	root, err := os.OpenRoot(rootName)
	if err != nil {
		return err
	}
	defer root.Close()
	return fs.WalkDir(root.FS(), ".", func(name string, entry fs.DirEntry, walkErr error) error {
		if walkErr != nil || name == "." {
			return walkErr
		}
		if !entry.IsDir() && !entry.Type().IsRegular() {
			return fmt.Errorf("non-regular entry %q", name)
		}
		if err := s.register(name, entry.IsDir()); err != nil || entry.IsDir() {
			return err
		}
		info, err := entry.Info()
		if err != nil {
			return err
		}
		return s.read(name, uint64(info.Size()), func() (io.ReadCloser, error) {
			f, err := root.Open(name)
			if err != nil {
				return nil, err
			}
			actual, err := f.Stat()
			if err != nil || !actual.Mode().IsRegular() {
				f.Close()
				return nil, fmt.Errorf("not a regular file: %q", name)
			}
			return f, nil
		})
	})
}

func scanZip(filename string, s *snapshot) error {
	f, err := os.Open(filename)
	if err != nil {
		return err
	}
	defer f.Close()
	info, err := f.Stat()
	if err != nil {
		return err
	}
	if info.Size() > MaxZipBytes {
		return errors.New("ZIP exceeds compressed size limit")
	}
	z, err := zip.NewReader(f, info.Size())
	if err != nil {
		return err
	}
	for _, f := range z.File {
		mode := f.Mode()
		dir := mode.IsDir()
		name := f.Name
		if dir {
			name = strings.TrimSuffix(name, "/")
			if f.UncompressedSize64 != 0 {
				return fmt.Errorf("ZIP directory has content: %q", name)
			}
		} else if !mode.IsRegular() {
			return fmt.Errorf("non-regular ZIP entry %q", name)
		}
		if err := s.register(name, dir); err != nil {
			return err
		}
		if dir {
			continue
		}
		// ceil(size / MaxRatio), without multiplying attacker-controlled sizes.
		size := f.UncompressedSize64
		minCompressed := size / MaxRatio
		if size%MaxRatio != 0 {
			minCompressed++
		}
		if f.CompressedSize64 < minCompressed {
			return fmt.Errorf("%q exceeds ZIP compression ratio limit", name)
		}
		if err := s.read(name, size, f.Open); err != nil {
			return err
		}
	}
	return nil
}

func scan(filename string) (*snapshot, error) {
	info, err := os.Lstat(filename)
	if err != nil {
		return nil, err
	}
	s := newSnapshot()
	switch {
	case info.IsDir():
		err = scanDir(filename, s)
	case info.Mode().IsRegular() && strings.EqualFold(path.Ext(filename), ".zip"):
		err = scanZip(filename, s)
	default:
		err = errors.New("expected a directory or a regular .zip")
	}
	return s, err
}
