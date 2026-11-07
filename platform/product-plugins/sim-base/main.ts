type StepInput = { step: { params: { label: string } } };

export function describe() {
  return {
    id: "org.tianbot.demo-base",
    products: [{
      id: "sim-base",
      title: "底盘连接替身",
      inputSchema: { type: "object", properties: { label: { type: "string" } } },
      form: { schemaVersion: 1, blocks: [
        { id: "connection", kind: "form", props: { fields: [
          { id: "label", kind: "text", label: "设备备注", required: true }
        ] } }
      ] },
      requiredChecks: ["identity"]
    }]
  };
}

export function plan(input: { inputs: { label: string } }) {
  return {
    schemaVersion: 1,
    steps: [{
      id: "identity", title: "识别设备", checkIds: ["identity"], dependsOn: [],
      effect: "read", resources: [], timeoutMs: 1000, maxAttempts: 1,
      requiredCapabilities: ["sim.base"], requiredGates: [], params: input.inputs
    }],
    requiredChecks: ["identity"],
    deliveryGates: []
  };
}

export async function execute(input: StepInput, host: { progress: (p: { message: string }) => void }) {
  const message: string = await Promise.resolve(input.step.params.label);
  host.progress({ message });
  return {
    schemaVersion: 1,
    checks: [{
      checkId: "identity", verdict: "PENDING", reasonCode: "SIMULATION_ONLY",
      message, measurements: [], criteria: null, artifactIds: []
    }]
  };
}
