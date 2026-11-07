package probe

import (
	"bytes"
	"strings"
	"testing"
)

func TestFrameBoundary(t *testing.T) {
	for _, n := range []int{MaxFrameBytes, MaxFrameBytes + 1} {
		frame := strings.Repeat(" ", n) + "\n"
		data, err := read(scanner(strings.NewReader(frame)))
		if n == MaxFrameBytes && (err != nil || len(data) != n) {
			t.Fatalf("exact limit: %d %v", len(data), err)
		}
		if n > MaxFrameBytes && err == nil {
			t.Fatal("accepted oversized frame")
		}
	}
	if _, err := read(scanner(strings.NewReader("{}"))); err == nil {
		t.Fatal("accepted unterminated frame")
	}
}

func TestHandshakeRejectsWrongMajor(t *testing.T) {
	const frame = `{"protocolMajor":1,"protocolMinor":0,"kind":"request","id":"hello","method":"probe.hello","params":{"protocolMajor":2,"protocolMinor":0,"maxFrameBytes":1048576,"capabilities":[]}}`
	var out bytes.Buffer
	if err := Serve(strings.NewReader(frame+"\n"), &out); err != nil {
		t.Fatal(err)
	}
	line, err := read(scanner(&out))
	if err != nil {
		t.Fatal(err)
	}
	reply, err := expectResponse(line, "hello")
	if err != nil || reply.Error == nil || reply.Error.Code != "INCOMPATIBLE" {
		t.Fatalf("reply: %s %v", line, err)
	}
}
