package probe

import (
	"encoding/json"
	"errors"
	"io"
	"time"

	"github.com/tianbot/tb-verify/platform/worker"
)

// Serve runs only in a disposable process. The input reader is never queued
// behind JS: it switches to control-only immediately after the first call.
func Serve(in io.Reader, out io.Writer) error {
	r, w := scanner(in), &writer{w: out}
	first, err := read(r)
	if err != nil {
		return err
	}
	hello, err := get[request](first, "Request")
	if err != nil {
		return err
	}
	if hello.Method != "probe.hello" {
		return w.reply(hello.ID, nil, fail("HANDSHAKE_REQUIRED", "expected probe.hello"))
	}
	type limits struct{ MaxFrameBytes int }
	peer, err := get[limits](hello.Params, "Handshake")
	if err != nil {
		return w.reply(hello.ID, nil, fail("INCOMPATIBLE", "unsupported handshake"))
	}
	// The P0 worker accepts peers that can receive the v1 frame budget.
	if peer.MaxFrameBytes != MaxFrameBytes {
		return w.reply(hello.ID, nil, fail("INCOMPATIBLE", "incompatible frame budget"))
	}
	if err := w.reply(hello.ID, handshake, nil); err != nil {
		return err
	}

	frame, err := read(r)
	if err != nil {
		return err
	}
	req, err := get[request](frame, "Request")
	if err != nil {
		return err
	}
	if req.Method != "probe.call" {
		return w.reply(req.ID, nil, fail("UNSUPPORTED", "unknown probe data method"))
	}
	if req.ProtocolMinor != 0 || req.ID == hello.ID {
		return w.reply(req.ID, nil, fail("INVALID_CALL", "invalid version or call ID"))
	}
	call, err := get[worker.Call](req.Params, "ProbeCall")
	if err != nil {
		return w.reply(req.ID, nil, fail("INVALID_CALL", "invalid probe call"))
	}

	evaluated := make(chan error, 1)
	go func() {
		value, err := worker.Eval(call, func(data json.RawMessage) error {
			return w.send(event{1, "event", req.ID, "1", "probe.progress", time.Now().UTC().Format(time.RFC3339Nano), data})
		})
		var reply *rpcError
		if errors.Is(err, worker.ErrMemory) {
			reply = fail("MEMORY_LIMIT", "MoeJS allocation budget exceeded")
		} else if err != nil {
			reply = fail("SCRIPT_ERROR", "plugin failed to return a valid result")
		}
		evaluated <- w.reply(req.ID, value, reply)
	}()

	control := make(chan error, 1)
	go func() {
		// Exactly one control message. Closing stdin also tears down the
		// disposable process, even when the engine goroutine cannot yield.
		frame, err := read(r)
		if err != nil {
			control <- err
			return
		}
		msg, err := get[request](frame, "Request")
		if err != nil {
			control <- err
			return
		}
		if msg.Method != "probe.cancel" {
			control <- w.reply(msg.ID, nil, fail("UNSUPPORTED", "unknown probe control method"))
			return
		}
		type cancel struct {
			CallID string `json:"callId"`
		}
		c, err := get[cancel](msg.Params, "ProbeCancel")
		if err != nil || msg.ProtocolMinor != 0 || c.CallID != req.ID || msg.ID == req.ID || msg.ID == hello.ID {
			control <- w.reply(msg.ID, nil, fail("INVALID_CONTROL", "control does not match the active call"))
			return
		}
		err = w.reply(msg.ID, raw(map[string]any{"callId": c.CallID, "accepted": true}), nil)
		// Keep the process alive on cancellation to exercise parent-owned
		// forced termination even if the engine's own interrupt path fails.
		if err == nil {
			_, err = read(r)
			if err == nil {
				err = errors.New("extra control frame")
			}
		}
		control <- err
	}()
	select {
	case err := <-evaluated:
		return err
	case err := <-control:
		if errors.Is(err, io.EOF) {
			return nil
		}
		return err
	}
}
