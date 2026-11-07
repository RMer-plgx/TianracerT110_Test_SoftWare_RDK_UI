// Command tb-p0 runs the isolated worker and plugin-contract experiments.
package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"runtime"
	"strings"
	"time"

	"github.com/tianbot/tb-verify/platform/plugincheck"
	"github.com/tianbot/tb-verify/platform/probe"
	"github.com/tianbot/tb-verify/platform/worker"
)

func main() {
	if err := run(os.Args[1:]); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

func run(args []string) error {
	switch {
	case len(args) == 1 && args[0] == "worker":
		return probe.Serve(os.Stdin, os.Stdout)
	case len(args) >= 2 && len(args) <= 3 && args[0] == "check":
		target := runtime.GOOS + "/" + runtime.GOARCH
		if len(args) == 3 {
			target = args[2]
		}
		result, err := plugincheck.Check(args[1], target)
		if err != nil {
			return err
		}
		return json.NewEncoder(os.Stdout).Encode(result)
	case len(args) == 2 && args[0] == "integrity":
		return plugincheck.WriteIntegrity(args[1], os.Stdout)
	case len(args) == 2 && args[0] == "smoke":
		b, err := readSource(args[1])
		if err != nil {
			return err
		}
		return smoke(string(b))
	default:
		return errors.New("usage: tb-p0 check <directory|zip> [os/arch] | integrity <directory> | smoke <sim-base/main.ts>")
	}
}

func readSource(path string) ([]byte, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	return io.ReadAll(io.LimitReader(f, worker.MaxSourceBytes+1))
}

const stepInput = `{"runId":"simulated-run","step":{"id":"identity","checkIds":["identity"],"title":"识别设备","dependsOn":[],"effect":"read","resources":[],"timeoutMs":1000,"maxAttempts":1,"requiredCapabilities":["sim.base"],"requiredGates":[],"params":{"label":"西安工位·底盘备注"}},"attempt":1,"deviceHandle":"mock","previousResults":[]}`

const prefix = `export function describe() {} export function plan() {} `
const spin = prefix + `export async function execute(input, host) { for (let first = true;; first = false) { if (first) host.progress({message: input.step.params.label}); } }`
const oom = prefix + `export async function execute() { const held = []; for (;;) { held.push({index: held.length}); await null; } }`

func smoke(ts string) error {
	self, err := os.Executable()
	if err != nil {
		return err
	}
	for _, test := range []struct {
		name, entry, source, code string
		timeout                   time.Duration
		options                   probe.Options
		kill, ack, value          bool
	}{
		{"typescript-async-utf8", "ts", ts, "OK", 5 * time.Second, probe.Options{}, false, false, true},
		{"moejs-allocation-limit", "js", oom, "MEMORY_LIMIT", 5 * time.Second, probe.Options{}, false, false, false},
		{"infinite-loop-deadline", "js", spin, "TIMED_OUT", 2 * time.Second, probe.Options{}, true, false, false},
		{"control-during-infinite-loop", "js", spin, "CANCELLED", 5 * time.Second, probe.Options{CancelOnProgress: true}, true, true, false},
		{"stdin-eof-during-infinite-loop", "js", spin, "PARENT_EOF", 5 * time.Second, probe.Options{CloseOnProgress: true}, false, false, false},
	} {
		ctx, cancel := context.WithTimeout(context.Background(), test.timeout)
		start := time.Now()
		out, err := probe.Run(ctx, self, worker.Call{Method: "execute", Entry: test.entry, Source: test.source, Input: json.RawMessage(stepInput)}, test.options)
		cancel()
		if err != nil {
			return fmt.Errorf("%s: %w", test.name, err)
		}
		result := struct {
			Probe     string `json:"probe"`
			ElapsedMS int64  `json:"elapsedMs"`
			probe.Outcome
		}{test.name, time.Since(start).Milliseconds(), out}
		if err := json.NewEncoder(os.Stdout).Encode(result); err != nil {
			return err
		}
		if out.Code != test.code || !out.ProcessExited || out.ForcedKill != test.kill || out.ControlAccepted != test.ack ||
			(test.value && !strings.Contains(string(out.Value), "西安工位·底盘备注")) ||
			(test.code != "MEMORY_LIMIT" && !strings.Contains(string(out.Progress), "西安工位·底盘备注")) {
			return fmt.Errorf("%s: unexpected outcome", test.name)
		}
	}
	return nil
}
