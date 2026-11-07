package protocol

import (
	"bytes"
	_ "embed"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"sync"
	"unicode/utf8"

	"github.com/santhosh-tekuri/jsonschema/v6"
)

//go:embed v1.schema.json
var schema []byte

const SchemaID = "https://tianbot.github.io/tb-verify/protocol/v1.schema.json"

// Schema is the sole source for the wire contract and generated TypeScript types.
func Schema() []byte { return bytes.Clone(schema) }

var definitions sync.Map

type offlineLoader struct{}

func (offlineLoader) Load(url string) (any, error) {
	return nil, fmt.Errorf("external schema references are disabled: %s", url)
}

func compiler(id string, doc any) (*jsonschema.Compiler, error) {
	c := jsonschema.NewCompiler()
	c.DefaultDraft(jsonschema.Draft2020)
	c.AssertFormat()
	c.UseLoader(offlineLoader{})
	return c, c.AddResource(id, doc)
}

func CompileInputSchema(data []byte) error {
	doc, err := Decode(data)
	if err != nil {
		return err
	}
	if _, ok := doc.(map[string]any); !ok {
		return errors.New("input schema must be an object")
	}
	const id = "urn:tb-verify:input-schema"
	c, err := compiler(id, doc)
	if err == nil {
		_, err = c.Compile(id)
	}
	return err
}

func Validate(name string, data []byte) error {
	value, err := Decode(data)
	if err != nil {
		return fmt.Errorf("%s: %w", name, err)
	}
	return ValidateValue(name, value)
}

func ValidateValue(name string, value any) error {
	v, ok := definitions.Load(name)
	if !ok {
		doc, err := Decode(schema)
		if err != nil {
			return err
		}
		c, err := compiler(SchemaID, doc)
		if err != nil {
			return err
		}
		v, err = c.Compile(SchemaID + "#/$defs/" + name)
		if err != nil {
			return err
		}
		v, _ = definitions.LoadOrStore(name, v)
	}
	if err := v.(*jsonschema.Schema).Validate(value); err != nil {
		return fmt.Errorf("%s: %w", name, err)
	}
	return nil
}

// Decode rejects ambiguous duplicate keys before either schema validation or
// unmarshalling into Go types. Limit nesting independently of the frame size.
func Decode(data []byte) (any, error) {
	if !utf8.Valid(data) {
		return nil, errors.New("invalid UTF-8")
	}
	d := json.NewDecoder(bytes.NewReader(data))
	d.UseNumber()
	v, err := valueAt(d, 0)
	if err != nil {
		return nil, err
	}
	if _, err := d.Token(); err != io.EOF {
		return nil, errors.New("expected exactly one JSON value")
	}
	return v, nil
}

func valueAt(d *json.Decoder, depth int) (any, error) {
	if depth >= 64 {
		return nil, errors.New("JSON nesting limit exceeded")
	}
	token, err := d.Token()
	if err != nil {
		return nil, err
	}
	switch token {
	case json.Delim('{'):
		out := map[string]any{}
		for d.More() {
			key, err := d.Token()
			if err != nil {
				return nil, err
			}
			name, ok := key.(string)
			if !ok {
				return nil, errors.New("expected object key")
			}
			if _, ok := out[name]; ok {
				return nil, fmt.Errorf("duplicate object key %q", name)
			}
			out[name], err = valueAt(d, depth+1)
			if err != nil {
				return nil, err
			}
		}
		_, err := d.Token()
		return out, err
	case json.Delim('['):
		out := []any{}
		for d.More() {
			item, err := valueAt(d, depth+1)
			if err != nil {
				return nil, err
			}
			out = append(out, item)
		}
		_, err := d.Token()
		return out, err
	default:
		if num, ok := token.(json.Number); ok {
			if _, err := num.Float64(); err != nil {
				return nil, errors.New("number is outside the finite JS range")
			}
		}
		return token, nil
	}
}
