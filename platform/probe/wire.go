// Package probe implements a disposable, one-call subprocess experiment.
// It has no persistent session state or device-control authority.
package probe

import (
	"bufio"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"sync"

	"github.com/tianbot/tb-verify/platform/protocol"
)

const MaxFrameBytes = 1 << 20

type request struct {
	ProtocolMajor int             `json:"protocolMajor"`
	ProtocolMinor int             `json:"protocolMinor"`
	Kind          string          `json:"kind"`
	ID            string          `json:"id"`
	Method        string          `json:"method"`
	Params        json.RawMessage `json:"params"`
}

type response struct {
	ProtocolMajor int             `json:"protocolMajor"`
	ProtocolMinor int             `json:"protocolMinor"`
	Kind          string          `json:"kind"`
	ID            string          `json:"id"`
	Result        json.RawMessage `json:"result,omitempty"`
	Error         *rpcError       `json:"error,omitempty"`
}

type rpcError struct {
	Code      string `json:"code"`
	Message   string `json:"message"`
	Retryable bool   `json:"retryable"`
	Details   any    `json:"details"`
}

func (e *rpcError) Error() string { return e.Code + ": " + e.Message }

type event struct {
	ProtocolMajor int             `json:"protocolMajor"`
	Kind          string          `json:"kind"`
	RunID         string          `json:"runId"`
	Seq           string          `json:"seq"`
	Type          string          `json:"type"`
	OccurredAt    string          `json:"occurredAt"`
	Payload       json.RawMessage `json:"payload"`
}

var handshake = json.RawMessage(`{"protocolMajor":1,"protocolMinor":0,"maxFrameBytes":1048576,"capabilities":[]}`)

func raw(v any) json.RawMessage {
	b, err := json.Marshal(v)
	if err != nil {
		panic(err) // internal value, not plugin-controlled
	}
	return b
}

func scanner(r io.Reader) *bufio.Scanner {
	s := bufio.NewScanner(r)
	// The split function requires a line terminator, which occupies one byte.
	s.Buffer(make([]byte, 4096), MaxFrameBytes+1)
	s.Split(func(b []byte, eof bool) (int, []byte, error) {
		a, token, err := bufio.ScanLines(b, eof)
		if err == nil && a > 0 && b[a-1] != '\n' {
			err = errors.New("unterminated JSON line")
		}
		return a, token, err
	})
	return s
}

func read(s *bufio.Scanner) ([]byte, error) {
	if s.Scan() {
		return s.Bytes(), nil
	}
	if err := s.Err(); err != nil {
		return nil, err
	}
	return nil, io.EOF
}

type writer struct {
	mu sync.Mutex
	w  io.Writer
}

func (w *writer) send(v any) error {
	b, err := json.Marshal(v)
	if err != nil {
		return err
	}
	if len(b) > MaxFrameBytes {
		return errors.New("frame exceeds limit")
	}
	w.mu.Lock()
	defer w.mu.Unlock()
	_, err = w.w.Write(append(b, '\n'))
	return err
}

func (w *writer) reply(id string, result json.RawMessage, failure *rpcError) error {
	return w.send(response{1, 0, "response", id, result, failure})
}

func fail(code, msg string) *rpcError { return &rpcError{Code: code, Message: msg} }

func get[T any](frame []byte, def string) (T, error) {
	var v T
	if err := protocol.Validate(def, frame); err != nil {
		return v, err
	}
	return v, json.Unmarshal(frame, &v)
}

func expectResponse(frame []byte, id string) (response, error) {
	r, err := get[response](frame, "Response")
	if err == nil && (r.ID != id || r.ProtocolMinor != 0) {
		err = fmt.Errorf("unexpected response %q / minor %d", r.ID, r.ProtocolMinor)
	}
	return r, err
}
