package plugincheck

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"path"
	"slices"
	"strings"

	"github.com/tianbot/tb-verify/platform/protocol"
	"github.com/tianbot/tb-verify/platform/worker"
	"golang.org/x/mod/semver"
)

type capability struct {
	major int
	read  string
}

var catalog = map[string]capability{
	"sim.base":   {1, "sim.base.read"},
	"sim.serial": {1, "sim.serial.read"},
}

type Result struct {
	ID            string `json:"id"`
	Version       string `json:"version"`
	ContentDigest string `json:"contentDigest"`
	Mode          string `json:"mode"`
}

type manifest struct {
	ID, Version, APIVersion, Entry, Integrity string
	Products                                  []string
	Requires                                  struct {
		Platforms    []string
		Capabilities []struct {
			ID    string
			Major int
		}
	}
	Permissions struct {
		DeviceCapabilities, NetworkHosts, HelperIDs []string
	}
	Helpers []struct {
		ID, Platform, Entry string
		Files               []string
	}
}

type integrityItem struct {
	Path   string `json:"path"`
	Size   int    `json:"size"`
	SHA256 string `json:"sha256"`
}

type product struct {
	ID          string
	InputSchema string
	Baselines   []struct{ ID, Revision, Path string }
	Checks      []struct{ ID, CriterionKey string }
}

type baseline struct {
	ID, ProductID, Revision string
	Criteria                map[string]json.RawMessage
}

func readAs[T any](s *snapshot, filename, def string) (T, error) {
	var out T
	data, ok := s.files[filename]
	if !ok {
		return out, fmt.Errorf("missing %s", filename)
	}
	if err := protocol.Validate(def, data); err != nil {
		return out, fmt.Errorf("%s: %w", filename, err)
	}
	return out, json.Unmarshal(data, &out)
}

func hash(data []byte) string {
	digest := sha256.Sum256(data)
	return hex.EncodeToString(digest[:])
}

func integrity(s *snapshot, filename string) (string, error) {
	m, err := readAs[struct{ Files []integrityItem }](s, filename, "Integrity")
	if err != nil {
		return "", err
	}
	seen := map[string]bool{}
	for _, item := range m.Files {
		if !safePath(item.Path) || item.Path == filename || seen[item.Path] {
			return "", fmt.Errorf("invalid integrity path %q", item.Path)
		}
		seen[item.Path] = true
		data, ok := s.files[item.Path]
		if !ok || len(data) != item.Size || hash(data) != item.SHA256 {
			return "", fmt.Errorf("integrity mismatch: %q", item.Path)
		}
	}
	for name := range s.files {
		if name != filename && !seen[name] {
			return "", fmt.Errorf("unlisted file: %q", name)
		}
	}
	slices.SortFunc(m.Files, func(a, b integrityItem) int { return strings.Compare(a.Path, b.Path) })
	normalized, err := json.Marshal(struct {
		Files []integrityItem `json:"files"`
	}{m.Files})
	if err != nil {
		return "", err
	}
	return hash(normalized), nil
}

func (m manifest) compatible(target string) error {
	v := "v" + m.Version
	if !semver.IsValid(v) || strings.SplitN(v, "+", 2)[0] != semver.Canonical(v) {
		return fmt.Errorf("invalid SemVer: %q", m.Version)
	}
	if m.APIVersion != "1.0" {
		return fmt.Errorf("unsupported plugin API %q", m.APIVersion)
	}
	if !slices.Contains(m.Requires.Platforms, target) {
		return fmt.Errorf("platform not declared: %s", target)
	}
	seen := map[string]bool{}
	operations := map[string]bool{}
	for _, required := range m.Requires.Capabilities {
		if seen[required.ID] {
			return fmt.Errorf("duplicate capability %q", required.ID)
		}
		seen[required.ID] = true
		c, ok := catalog[required.ID]
		if !ok || c.major != required.Major {
			return fmt.Errorf("missing capability %s@%d", required.ID, required.Major)
		}
		operations[c.read] = true
	}
	for _, permission := range m.Permissions.DeviceCapabilities {
		if !operations[permission] {
			return fmt.Errorf("unauthorized capability permission %q", permission)
		}
	}
	if len(m.Permissions.NetworkHosts) != 0 || len(m.Permissions.HelperIDs) != 0 {
		return errors.New("P0 check only supports in-memory capabilities; network/helper permissions unavailable")
	}
	return nil
}

func (m manifest) validateHelpers(s *snapshot) error {
	seen := map[[2]string]bool{}
	for _, helper := range m.Helpers {
		key := [2]string{helper.ID, helper.Platform}
		if seen[key] || !slices.Contains(m.Requires.Platforms, helper.Platform) ||
			!slices.Contains(helper.Files, helper.Entry) {
			return fmt.Errorf("invalid helper declaration %q", helper.ID)
		}
		seen[key] = true
		for _, name := range helper.Files {
			if _, ok := s.files[name]; !ok {
				return fmt.Errorf("missing helper file %q", name)
			}
		}
	}
	return nil
}

func validateProduct(s *snapshot, p product) error {
	input, ok := s.files[p.InputSchema]
	if !ok {
		return fmt.Errorf("missing input schema: %q", p.InputSchema)
	}
	if err := protocol.CompileInputSchema(input); err != nil {
		return fmt.Errorf("%s: %w", p.InputSchema, err)
	}
	checkIDs := map[string]bool{}
	for _, check := range p.Checks {
		if checkIDs[check.ID] {
			return fmt.Errorf("duplicate check ID %q", check.ID)
		}
		checkIDs[check.ID] = true
	}
	seen := map[[2]string]bool{}
	for _, ref := range p.Baselines {
		b, err := readAs[baseline](s, ref.Path, "Baseline")
		if err != nil {
			return err
		}
		key := [2]string{ref.ID, ref.Revision}
		if seen[key] || b.ID != ref.ID || b.Revision != ref.Revision || b.ProductID != p.ID {
			return fmt.Errorf("baseline reference mismatch: %q", ref.Path)
		}
		seen[key] = true
		for _, check := range p.Checks {
			if _, ok := b.Criteria[check.CriterionKey]; !ok {
				return fmt.Errorf("%s: missing criterion %q for %q", ref.Path, check.CriterionKey, check.ID)
			}
		}
	}
	return nil
}

func Check(filename, target string) (Result, error) {
	s, err := scan(filename)
	if err != nil {
		return Result{}, err
	}
	m, err := readAs[manifest](s, "plugin.json", "Manifest")
	if err != nil {
		return Result{}, err
	}
	digest, err := integrity(s, m.Integrity)
	if err != nil {
		return Result{}, err
	}
	if err := m.compatible(target); err != nil {
		return Result{}, err
	}
	if err := m.validateHelpers(s); err != nil {
		return Result{}, err
	}
	productIDs := map[string]bool{}
	for _, name := range m.Products {
		p, err := readAs[product](s, name, "ProductDefinition")
		if err != nil {
			return Result{}, err
		}
		if productIDs[p.ID] {
			return Result{}, fmt.Errorf("duplicate product ID %q", p.ID)
		}
		productIDs[p.ID] = true
		if err := validateProduct(s, p); err != nil {
			return Result{}, err
		}
	}
	entry, ok := s.files[m.Entry]
	if !ok {
		return Result{}, fmt.Errorf("missing entry %q", m.Entry)
	}
	if _, err := worker.Compile(strings.TrimPrefix(path.Ext(m.Entry), "."), entry); err != nil {
		return Result{}, err
	}
	return Result{m.ID, m.Version, digest, "development"}, nil
}

// WriteIntegrity is for fixture authors; it does not install or approve a package.
func WriteIntegrity(filename string, w io.Writer) error {
	s, err := scan(filename)
	if err != nil {
		return err
	}
	items := []integrityItem{}
	for name, data := range s.files {
		if name != "integrity.json" {
			items = append(items, integrityItem{name, len(data), hash(data)})
		}
	}
	slices.SortFunc(items, func(a, b integrityItem) int { return strings.Compare(a.Path, b.Path) })
	enc := json.NewEncoder(w)
	enc.SetIndent("", "  ")
	return enc.Encode(struct {
		Files []integrityItem `json:"files"`
	}{items})
}
