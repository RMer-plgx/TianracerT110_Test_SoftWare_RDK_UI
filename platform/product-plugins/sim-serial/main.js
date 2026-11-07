// Configuration-shaped fixture with a prebundled entry, no SDK code generator.
export function describe() {
  return {
    id: "org.tianbot.demo-serial",
    products: [{
      id: "sim-serial", title: "串口协议合成夹具",
      inputSchema: { type: "object", properties: { channel: { type: "string" } } },
      form: { schemaVersion: 1, blocks: [
        { id: "connection", kind: "form", props: { fields: [
          { id: "channel", kind: "device", label: "虚拟通道", required: true }
        ] } }
      ] },
      requiredChecks: ["frame"]
    }]
  };
}
export function plan(input) {
  return {
    schemaVersion: 1,
    steps: [{
      id: "frame", title: "校验合成帧", checkIds: ["frame"], dependsOn: [],
      effect: "read", resources: [], timeoutMs: 1000, maxAttempts: 1,
      requiredCapabilities: ["sim.serial"], requiredGates: [], params: input.inputs
    }],
    requiredChecks: ["frame"], deliveryGates: []
  };
}
export async function execute() {
  return {
    schemaVersion: 1, checks: [{
      checkId: "frame", verdict: "PENDING", reasonCode: "SIMULATION_ONLY",
      message: "合成帧夹具", measurements: [], criteria: null, artifactIds: []
    }]
  };
}
