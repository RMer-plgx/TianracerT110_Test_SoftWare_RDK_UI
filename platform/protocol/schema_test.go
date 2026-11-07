package protocol

import (
	"strings"
	"testing"
)

func TestWireAndViewFixtures(t *testing.T) {
	for _, test := range []struct {
		name, def, data string
		reject          bool
	}{
		{"cancel request", "Request", `{"protocolMajor":1,"protocolMinor":0,"kind":"request","id":"req-17","method":"runs.cancel","idempotencyKey":"cancel-run-demo-1","params":{"runId":"run-demo-1","reason":"operator_requested"}}`, false},
		{"major mismatch", "Request", `{"protocolMajor":2,"protocolMinor":0,"kind":"request","id":"req-17","method":"runs.cancel","params":{}}`, true},
		{"duplicate field", "Request", `{"protocolMajor":1,"protocolMinor":0,"kind":"request","id":"req-17","method":"a","method":"b","params":{}}`, true},
		{"result null", "Response", `{"protocolMajor":1,"protocolMinor":0,"kind":"response","id":"a","result":null}`, false},
		{"two terminal outcomes", "Response", `{"protocolMajor":1,"protocolMinor":0,"kind":"response","id":"a","result":null,"error":{"code":"UNSUPPORTED","message":"unknown method","retryable":false,"details":null}}`, true},
		{"large sequence string", "Event", `{"protocolMajor":1,"kind":"event","runId":"r","seq":"9007199254740999","type":"step.state_changed","stepId":"s","attempt":1,"occurredAt":"2026-11-07T08:00:00Z","payload":{"state":"SUCCEEDED"}}`, false},
		{"invalid state", "Event", `{"protocolMajor":1,"kind":"event","runId":"r","seq":"1","type":"step.state_changed","occurredAt":"2026-11-07T08:00:00Z","payload":{"state":"PASS"}}`, true},
		{"numeric sequence", "Event", `{"protocolMajor":1,"kind":"event","runId":"r","seq":9007199254740999,"type":"step.state_changed","occurredAt":"2026-11-07T08:00:00Z","payload":{"state":"SUCCEEDED"}}`, true},
		{"view form", "ViewSpec", `{"schemaVersion":1,"blocks":[{"id":"a","kind":"form","props":{"fields":[{"id":"operator","kind":"text","label":"操作员","required":true}]}}]}`, false},
		{"image cannot fetch URL", "ViewSpec", `{"schemaVersion":1,"blocks":[{"id":"a","kind":"image","props":{"source":{"kind":"asset","path":"https://example.com/a.png"},"alt":"example"}}]}`, true},
		{"image path traversal", "ViewSpec", `{"schemaVersion":1,"blocks":[{"id":"a","kind":"image","props":{"source":{"kind":"asset","path":"assets/../a.png"},"alt":"example"}}]}`, true},
		{"unknown required block", "ViewSpec", `{"schemaVersion":1,"blocks":[{"id":"a","kind":"webview","props":{}}]}`, true},
		{"host invoke", "HostInvokeInput", `{"handle":"opaque","capability":"sim.base","operation":"read","params":null,"timeoutMs":100}`, false},
		{"host invoke missing handle", "HostInvokeInput", `{"capability":"sim.base","operation":"read","params":null,"timeoutMs":100}`, true},
		{"unbounded measurement", "Measurement", `{"key":"m","value":1e999,"unit":"V","quality":"valid","observedAt":"2026-11-07T08:00:00Z","artifactIds":[]}`, true},
		{"adapter handshake", "AdapterDescriptor", `{"protocolMajor":1,"protocolMinor":0,"platform":"darwin/arm64","capabilities":[{"id":"sim.serial","major":1,"operations":{"read":{"effect":"read","paramsSchema":{}}}}],"resourceScopes":["fixture"],"stopGuarantees":[]}`, false},
	} {
		t.Run(test.name, func(t *testing.T) {
			if err := Validate(test.def, []byte(test.data)); (err != nil) != test.reject {
				t.Fatalf("reject=%t, got %v", test.reject, err)
			}
		})
	}
}

func TestNoNetworkSchemaRefs(t *testing.T) {
	err := CompileInputSchema([]byte(`{"$ref":"https://example.com/schema"}`))
	if err == nil || !strings.Contains(err.Error(), "external schema references are disabled") {
		t.Fatalf("expected offline error, got %v", err)
	}
}
