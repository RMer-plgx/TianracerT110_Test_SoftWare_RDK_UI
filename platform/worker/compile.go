package worker

import (
	"errors"
	"fmt"
	"slices"
	"unicode/utf8"

	"github.com/MoeForge/moejs"
)

const MaxSourceBytes = 64 << 10

var hooks = []string{"describe", "plan", "execute"}

// Compile checks the bundled entry without evaluating its top level.
func Compile(entry string, source []byte) (*moejs.Module, error) {
	if len(source) > MaxSourceBytes {
		return nil, errors.New("entry exceeds source limit")
	}
	if !utf8.Valid(source) {
		return nil, errors.New("invalid entry UTF-8")
	}
	var compile func(string, string) (*moejs.Module, error)
	switch entry {
	case "js":
		compile = moejs.Compile
	case "ts":
		compile = moejs.CompileTS
	default:
		return nil, errors.New("entry must be a .js or .ts module")
	}
	m, err := compile("entry."+entry, string(source))
	if err != nil {
		return nil, err
	}
	if len(m.Requests()) != 0 {
		return nil, errors.New("external module imports are not supported")
	}
	for _, name := range hooks {
		if !slices.Contains(m.Exports(), name) {
			return nil, fmt.Errorf("missing required export %s", name)
		}
	}
	return m, nil
}
