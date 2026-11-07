package probe

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os/exec"
	"time"

	"github.com/tianbot/tb-verify/platform/protocol"
	"github.com/tianbot/tb-verify/platform/worker"
)

type Options struct {
	// Fault injection for the P0 probe, triggered by the first progress
	// event sent from inside JS. The default waits for the result.
	CancelOnProgress bool
	CloseOnProgress  bool
}

type Outcome struct {
	Code            string          `json:"code"`
	Value           json.RawMessage `json:"value,omitempty"`
	Progress        json.RawMessage `json:"progress,omitempty"`
	ControlAccepted bool            `json:"controlAccepted"`
	ProcessExited   bool            `json:"processExited"`
	ForcedKill      bool            `json:"forcedKill"`
}

func Run(ctx context.Context, executable string, call worker.Call, opts Options) (out Outcome, err error) {
	if len(call.Source) > worker.MaxSourceBytes || !json.Valid(call.Input) {
		return out, errors.New("invalid probe input or source length")
	}
	ctx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, executable, "worker")
	in, err := cmd.StdinPipe()
	if err != nil {
		return out, err
	}
	defer in.Close()
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return out, err
	}
	defer stdout.Close()
	if err := cmd.Start(); err != nil {
		return out, err
	}
	finished := false
	defer func() {
		if !finished {
			if ctx.Err() == nil {
				out.ForcedKill = cmd.Process.Kill() == nil
			}
			_ = in.Close()
		}
		waitErr := cmd.Wait()
		out.ProcessExited = cmd.ProcessState != nil // Wait reaped the child, including signal exits.
		if errors.Is(ctx.Err(), context.DeadlineExceeded) && out.Code == "" {
			out.Code, out.ForcedKill, err = "TIMED_OUT", waitErr != nil, nil
		} else if ctx.Err() != nil && out.Code == "" {
			err = ctx.Err()
		} else if finished && waitErr != nil && err == nil {
			err = fmt.Errorf("worker exited unsuccessfully: %w", waitErr)
		}
	}()

	r, w := scanner(stdout), &writer{w: in}
	if err := w.send(request{1, 0, "request", "hello", "probe.hello", handshake}); err != nil {
		return out, err
	}
	line, err := read(r)
	if err != nil {
		return out, err
	}
	hello, err := expectResponse(line, "hello")
	if err != nil {
		return out, err
	}
	if hello.Error != nil {
		return out, hello.Error
	}
	if err := protocol.Validate("Handshake", hello.Result); err != nil {
		return out, err
	}
	if err := w.send(request{1, 0, "request", "call", "probe.call", raw(call)}); err != nil {
		return out, err
	}

	var controlWriteErr error
	for {
		line, err := read(r)
		if errors.Is(err, io.EOF) && opts.CloseOnProgress && out.Progress != nil {
			out.Code, finished = "PARENT_EOF", true
			return out, nil
		}
		if err != nil {
			return out, errors.Join(controlWriteErr, err)
		}
		var kind struct{ Kind string }
		if err := json.Unmarshal(line, &kind); err != nil {
			return out, err
		}
		if kind.Kind == "event" {
			e, err := get[event](line, "Event")
			if err != nil {
				return out, err
			}
			if e.RunID != "call" || e.Seq != "1" || e.Type != "probe.progress" || out.Progress != nil {
				return out, errors.New("unexpected progress event")
			}
			if err := protocol.Validate("HostProgressInput", e.Payload); err != nil {
				return out, err
			}
			out.Progress = e.Payload
			switch {
			case opts.CancelOnProgress:
				if err := w.send(request{1, 0, "request", "cancel", "probe.cancel", json.RawMessage(`{"callId":"call"}`)}); err != nil {
					// A fast worker can close stdin after writing its terminal
					// response. Keep draining stdout even if the control write fails.
					controlWriteErr = fmt.Errorf("send cancel: %w", err)
				}
			case opts.CloseOnProgress:
				if err := in.Close(); err != nil {
					return out, err
				}
			}
			continue
		}
		if opts.CloseOnProgress && out.Progress != nil {
			return out, errors.New("unexpected output after parent EOF")
		}
		reply, err := get[response](line, "Response")
		if err != nil {
			return out, err
		}
		if reply.ProtocolMinor != 0 {
			return out, fmt.Errorf("unexpected response minor %d", reply.ProtocolMinor)
		}
		// Frames are ordered on stdout. A call terminal before a valid cancel
		// acknowledgement wins; an acknowledgement first revokes the call and
		// no later output can publish a result.
		if reply.ID == "cancel" && opts.CancelOnProgress && out.Progress != nil {
			if reply.Error != nil {
				return out, reply.Error
			}
			a, err := get[struct{ CallID string }](reply.Result, "ProbeControlAck")
			if err != nil || a.CallID != "call" {
				return out, errors.New("invalid control acknowledgement")
			}
			out.Code, out.ControlAccepted = "CANCELLED", true
			return out, nil
		}
		if reply.ID != "call" {
			return out, fmt.Errorf("unexpected response %q", reply.ID)
		}
		finished = true
		if reply.Error != nil {
			out.Code = reply.Error.Code
		} else {
			out.Code, out.Value = "OK", reply.Result
		}
		return out, nil
	}
}
