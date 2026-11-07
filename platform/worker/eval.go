package worker

import (
	"encoding/json"
	"errors"
	"fmt"

	"github.com/MoeForge/moejs"
	"github.com/MoeForge/moejs/engine"
	"github.com/tianbot/tb-verify/platform/protocol"
)

const MemoryLimit = 8 << 20

type Call struct {
	Method string          `json:"method"`
	Entry  string          `json:"entry"`
	Source string          `json:"source"`
	Input  json.RawMessage `json:"input"`
}

var ErrMemory = moejs.ErrMemoryLimit

// Eval executes one export in a fresh VM. No Node, network, filesystem,
// credentials, helper or device APIs are registered.
func Eval(call Call, progress func(json.RawMessage) error) (json.RawMessage, error) {
	mod, err := Compile(call.Entry, []byte(call.Source))
	if err != nil {
		return nil, err
	}
	rt := moejs.NewRuntime(moejs.Options{MemoryLimit: MemoryLimit, DisableDynamicCode: true, Locale: "zh-CN"})
	if err := rt.Load(mod); err != nil {
		return nil, err
	}
	for _, name := range hooks {
		hook, err := mod.Hook(name)
		if err != nil {
			return nil, err
		}
		ok, err := rt.Has(hook)
		if err != nil {
			return nil, err
		}
		if !ok {
			return nil, fmt.Errorf("export %s is not callable", name)
		}
	}

	var args []moejs.Value
	var outputDef string
	switch call.Method {
	case "describe":
		outputDef = "PluginDescriptor"
	case "plan", "execute":
		inputDef := "PlanInput"
		outputDef = "Plan"
		if call.Method == "execute" {
			inputDef, outputDef = "StepInput", "StepResult"
		}
		if err := protocol.Validate(inputDef, call.Input); err != nil {
			return nil, err
		}
		input, err := rt.ParseJSON(call.Input)
		if err != nil {
			return nil, err
		}
		args = append(args, input)
		if call.Method == "execute" {
			host, err := progressStub(rt, progress)
			if err != nil {
				return nil, err
			}
			args = append(args, host)
		}
	default:
		return nil, errors.New("unsupported probe export")
	}

	hook, err := mod.Hook(call.Method)
	if err != nil {
		return nil, err
	}
	v, err := rt.Call(hook, args...)
	if err != nil {
		return nil, err
	}
	state, result, promise := moejs.PromiseResult(v)
	if call.Method == "execute" {
		if !promise || state != moejs.PromiseFulfilled {
			return nil, errors.New("execute did not return a fulfilled Promise")
		}
		v = result
	} else if promise {
		return nil, errors.New("describe and plan must return synchronously")
	}
	raw, err := rt.AppendJSONWith(nil, v, moejs.JSONOptions{Strict: true})
	if err != nil {
		return nil, err
	}
	if len(raw) > 256<<10 {
		return nil, errors.New("probe result exceeds limit")
	}
	if err := protocol.Validate(outputDef, raw); err != nil {
		return nil, err
	}
	return json.RawMessage(raw), nil
}

func progressStub(rt *moejs.Runtime, emit func(json.RawMessage) error) (moejs.Value, error) {
	realm := rt.Realm()
	host := realm.NewObject()
	sent := false
	fn := rt.Function("progress", 1, func(_ *moejs.Realm, _ moejs.Value, args []moejs.Value) (moejs.Value, error) {
		if sent || emit == nil {
			return moejs.Undefined(), nil
		}
		raw, err := rt.AppendJSONWith(nil, moejs.Arg(args, 0), moejs.JSONOptions{Strict: true})
		if err != nil {
			return moejs.Undefined(), err
		}
		if len(raw) > 4096 {
			return moejs.Undefined(), errors.New("probe progress exceeds limit")
		}
		if err := protocol.Validate("HostProgressInput", raw); err != nil {
			return moejs.Undefined(), err
		}
		sent = true
		return moejs.Undefined(), emit(raw)
	})
	err := host.SetProp(realm, realm.KeyFromGoString("progress"), fn)
	return engine.ObjectValue(host), err
}
