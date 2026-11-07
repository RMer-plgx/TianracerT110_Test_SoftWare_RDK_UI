package worker

import (
	"encoding/json"
	"errors"
	"os"
	"strings"
	"testing"

	"github.com/MoeForge/moejs"
)

func TestFixtureHooks(t *testing.T) {
	for _, test := range []struct{ id, entry, inputs string }{
		{"sim-base", "ts", `{"label":"底盘替身"}`},
		{"sim-serial", "js", `{"channel":"串口替身"}`},
	} {
		t.Run(test.id, func(t *testing.T) {
			dir := "../product-plugins/" + test.id
			source, err := os.ReadFile(dir + "/main." + test.entry)
			if err != nil {
				t.Fatal(err)
			}
			descriptor, err := Eval(Call{"describe", test.entry, string(source), json.RawMessage("null")}, nil)
			if err != nil || !strings.Contains(string(descriptor), test.id) {
				t.Fatalf("describe: %s %v", descriptor, err)
			}
			baseline, err := os.ReadFile(dir + "/baselines/simulation.json")
			if err != nil {
				t.Fatal(err)
			}
			args := json.RawMessage(`{"productId":"` + test.id + `","inputs":` + test.inputs + `,"baseline":` + string(baseline) + `,"capabilityVersions":{}}`)
			plan, err := Eval(Call{"plan", test.entry, string(source), args}, nil)
			if err != nil {
				t.Fatal(err)
			}
			var data struct{ Steps []json.RawMessage }
			if err := json.Unmarshal(plan, &data); err != nil || len(data.Steps) != 1 {
				t.Fatalf("plan: %s %v", plan, err)
			}
			input := json.RawMessage(`{"runId":"simulated","step":` + string(data.Steps[0]) + `,"attempt":1,"deviceHandle":"mock","previousResults":[]}`)
			value, err := Eval(Call{"execute", test.entry, string(source), input}, nil)
			if err != nil || !strings.Contains(string(value), `"verdict":"PENDING"`) {
				t.Fatalf("execute: %s %v", value, err)
			}
		})
	}
}

func TestNoPrivilegedGlobals(t *testing.T) {
	const source = `
export function plan() {}
export async function execute() {}
export function describe() {
  if (typeof process !== "undefined" || typeof require !== "undefined" || typeof fetch !== "undefined") {
    throw new Error("unexpected host API");
  }
  let disabled = false;
  try { eval("1+1"); } catch (e) { disabled = e.name === "EvalError"; }
  if (!disabled) throw new Error("eval enabled");
  return {id:"test", products:[{id:"test", title:"test", inputSchema:{}, form:{schemaVersion:1,blocks:[]}, requiredChecks:[]}]};
}`
	if _, err := Eval(Call{"describe", "js", source, json.RawMessage("null")}, nil); err != nil {
		t.Fatal(err)
	}
}

func TestMeasurementValuesAreStrictJSON(t *testing.T) {
	const input = `{"runId":"r","step":{"id":"s","checkIds":["voltage"],"title":"measure","dependsOn":[],"effect":"read","resources":[],"timeoutMs":1000,"maxAttempts":1,"requiredCapabilities":[],"requiredGates":[],"params":null},"attempt":1,"deviceHandle":"mock","previousResults":[]}`
	for _, c := range []struct {
		name, value, quality, expected string
	}{
		{"nan", "NaN", "valid", "NaN"},
		{"infinity", "Infinity", "valid", "Infinity"},
		{"negative infinity", "-Infinity", "valid", "Infinity"},
		{"undefined", "undefined", "valid", "undefined"},
		{"function", "() => 12", "valid", "function"},
		{"valid null", "null", "valid", "Measurement"},
		{"missing null", "null", "missing", ""},
		{"finite zero", "0", "valid", ""},
	} {
		t.Run(c.name, func(t *testing.T) {
			source := `export function describe() {} export function plan() {}
export async function execute() {
  return {schemaVersion:1,checks:[{checkId:"voltage",verdict:"PASS",reasonCode:"SAMPLE",message:"voltage",
    measurements:[{key:"voltage",value:` + c.value + `,unit:"V",quality:"` + c.quality + `",
    observedAt:"2026-11-07T08:00:00Z",artifactIds:[]}],criteria:null,artifactIds:[]}]};
}`
			out, err := Eval(Call{"execute", "js", source, json.RawMessage(input)}, nil)
			switch {
			case c.expected == "" && (err != nil || len(out) == 0):
				t.Fatalf("valid measurement rejected: %s %v", out, err)
			case c.expected == "Measurement" && (err == nil || !strings.Contains(err.Error(), "/value")):
				t.Fatalf("expected a schema error on value, got %s %v", out, err)
			case c.expected != "" && c.expected != "Measurement":
				var lossy *moejs.LossyJSONError
				if len(out) != 0 || !errors.As(err, &lossy) || !strings.Contains(lossy.Error(), c.expected) {
					t.Fatalf("expected lossy JSON rejection for %s, got %s %v", c.expected, out, err)
				}
			}
		})
	}
}
