// Command schema-ts projects the project's JSON Schema subset onto TS types.
// Runtime constraints (formats, patterns, numeric ranges, etc.) remain in JSON Schema.
package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"log"
	"os"
	"slices"
	"strings"

	"github.com/tianbot/tb-verify/platform/protocol"
)

type schema struct {
	Ref      string            `json:"$ref"`
	Type     string            `json:"type"`
	Const    json.RawMessage   `json:"const"`
	Enum     []json.RawMessage `json:"enum"`
	OneOf    []schema          `json:"oneOf"`
	Items    *schema           `json:"items"`
	Props    map[string]schema `json:"properties"`
	Required []string          `json:"required"`
	Extra    json.RawMessage   `json:"additionalProperties"`
	Defs     map[string]schema `json:"$defs"`
}

func (s schema) ts() string {
	switch {
	case s.Ref != "":
		name, ok := strings.CutPrefix(s.Ref, "#/$defs/")
		if !ok {
			log.Fatalf("unsupported type reference %q", s.Ref)
		}
		return name
	case s.Const != nil:
		return string(s.Const)
	case len(s.Enum) != 0:
		out := make([]string, 0, len(s.Enum))
		for _, v := range s.Enum {
			out = append(out, string(v))
		}
		return strings.Join(out, " | ")
	case len(s.OneOf) != 0:
		out := make([]string, 0, len(s.OneOf))
		for _, branch := range s.OneOf {
			out = append(out, branch.ts())
		}
		return strings.Join(out, " | ")
	}
	switch s.Type {
	case "array":
		return "Array<" + s.Items.ts() + ">"
	case "object":
		if string(s.Extra) == "false" {
			if len(s.Props) == 0 {
				return "Record<string, never>"
			}
			var out []string
			for _, key := range sorted(s.Props) {
				marker := ""
				if !slices.Contains(s.Required, key) {
					marker = "?"
				}
				out = append(out, fmt.Sprintf("%q%s: %s", key, marker, s.Props[key].ts()))
			}
			return "{ " + strings.Join(out, "; ") + " }"
		}
		var item schema
		if len(s.Props) > 0 || json.Unmarshal(s.Extra, &item) != nil {
			log.Fatal("unsupported object schema")
		}
		// Index signature preserves recursive Json without a generic alias.
		return "{ [key: string]: " + item.ts() + " }"
	case "integer", "number":
		return "number"
	case "string", "boolean", "null":
		return s.Type
	default:
		log.Fatalf("unsupported schema type %q", s.Type)
		return ""
	}
}

func sorted(m map[string]schema) []string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	slices.Sort(keys)
	return keys
}

func main() {
	if len(os.Args) < 2 || len(os.Args) > 3 || (len(os.Args) == 3 && os.Args[2] != "--check") {
		log.Fatal("usage: schema-ts path [--check]")
	}
	var doc schema
	if err := json.Unmarshal(protocol.Schema(), &doc); err != nil {
		log.Fatal(err)
	}
	var out bytes.Buffer
	out.WriteString("// Code generated from protocol/v1.schema.json by go generate ./protocol. DO NOT EDIT.\n\n")
	for _, name := range sorted(doc.Defs) {
		fmt.Fprintf(&out, "export type %s = %s;\n", name, doc.Defs[name].ts())
	}
	if len(os.Args) == 3 {
		data, err := os.ReadFile(os.Args[1])
		if err != nil || !bytes.Equal(data, out.Bytes()) {
			log.Fatal("generated TypeScript differs; run go generate ./protocol")
		}
		return
	}
	if err := os.WriteFile(os.Args[1], out.Bytes(), 0644); err != nil {
		log.Fatal(err)
	}
}
