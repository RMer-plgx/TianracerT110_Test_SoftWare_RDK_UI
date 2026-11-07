package probe

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/tianbot/tb-verify/platform/worker"
)

const testWorkerMode = "TB_P0_TEST_WORKER_MODE"

func TestMain(m *testing.M) {
	if len(os.Args) == 2 && os.Args[1] == "worker" {
		var err error
		if mode := os.Getenv(testWorkerMode); mode == "real" {
			err = Serve(os.Stdin, os.Stdout)
		} else {
			err = scriptedWorker(mode)
		}
		if err != nil {
			fmt.Fprintln(os.Stderr, err)
			os.Exit(1)
		}
		os.Exit(0)
	}
	os.Exit(m.Run())
}

func scriptedWorker(mode string) error {
	r, w := scanner(os.Stdin), &writer{w: os.Stdout}
	if _, err := read(r); err != nil {
		return err
	}
	if err := w.reply("hello", handshake, nil); err != nil {
		return err
	}
	if _, err := read(r); err != nil {
		return err
	}
	// Guarantee a broken control pipe before parent sees progress.
	if mode != "ack-first" {
		if err := os.Stdin.Close(); err != nil {
			return err
		}
	}
	if err := w.send(event{1, "event", "call", "1", "probe.progress", time.Now().UTC().Format(time.RFC3339Nano),
		json.RawMessage(`{"message":"before terminal"}`)}); err != nil {
		return err
	}
	if mode == "no-terminal" {
		return nil
	}
	if mode == "ack-first" {
		if _, err := read(r); err != nil {
			return err
		}
		if err := w.reply("cancel", json.RawMessage(`{"callId":"call","accepted":true}`), nil); err != nil {
			return err
		}
	}
	return w.reply("call", json.RawMessage(`{"winner":"call"}`), nil)
}

func TestCancelAndCompletionOrdering(t *testing.T) {
	exe, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	for _, mode := range []string{"call-first", "ack-first", "no-terminal"} {
		t.Run(mode, func(t *testing.T) {
			t.Setenv(testWorkerMode, mode)
			t.Setenv("GORACE", "atexit_sleep_ms=0")
			ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
			defer cancel()
			out, err := Run(ctx, exe, worker.Call{Input: json.RawMessage("null")}, Options{CancelOnProgress: true})
			if !out.ProcessExited {
				t.Fatalf("worker wasn't reaped: %+v %v", out, err)
			}
			switch mode {
			case "call-first":
				if err != nil || out.Code != "OK" || out.ControlAccepted || string(out.Value) != `{"winner":"call"}` {
					t.Fatalf("lost completed result after broken control pipe: %+v %v", out, err)
				}
			case "ack-first":
				if err != nil || out.Code != "CANCELLED" || !out.ControlAccepted || out.Value != nil {
					t.Fatalf("late result escaped cancellation: %+v %v", out, err)
				}
			case "no-terminal":
				if err == nil || !strings.Contains(err.Error(), "send cancel:") || !strings.Contains(err.Error(), io.EOF.Error()) || out.Code != "" {
					t.Fatalf("lost failed write or claimed a terminal: %+v %v", out, err)
				}
			}
		})
	}
}

func TestCancelAgainstFastTSWorker(t *testing.T) {
	t.Setenv(testWorkerMode, "real")
	t.Setenv("GORACE", "atexit_sleep_ms=0")
	exe, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	source, err := os.ReadFile("../product-plugins/sim-base/main.ts")
	if err != nil {
		t.Fatal(err)
	}
	const input = `{"runId":"r","step":{"id":"s","checkIds":["identity"],"title":"check","dependsOn":[],"effect":"read","resources":[],"timeoutMs":1000,"maxAttempts":1,"requiredCapabilities":[],"requiredGates":[],"params":{"label":"快速结果"}},"attempt":1,"deviceHandle":"mock","previousResults":[]}`
	out, err := Run(t.Context(), exe, worker.Call{Method: "execute", Entry: "ts", Source: string(source), Input: json.RawMessage(input)}, Options{CancelOnProgress: true})
	if err != nil || !out.ProcessExited || len(out.Progress) == 0 {
		t.Fatalf("protocol failure: %+v %v", out, err)
	}
	if (out.Code != "OK" || !strings.Contains(string(out.Value), "快速结果")) &&
		(out.Code != "CANCELLED" || !out.ControlAccepted || out.Value != nil) {
		t.Fatalf("unexpected terminal: %+v", out)
	}
}
