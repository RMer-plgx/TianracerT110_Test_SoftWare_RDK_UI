export * from "./protocol-v1.ts";

import type {
  HostProgressInput, HostLogInput, HostInvokeInput, HostInvokeResult,
  HostPromptInput, HostPromptResult, HostSaveJsonInput, HostSaveJsonResult,
  PluginDescriptor, PlanInput, Plan, StepInput, StepResult,
} from "./protocol-v1.ts";

// SDK signatures describe v1; the P0 worker only exposes an in-memory progress stub.
export interface Host {
  progress(event: HostProgressInput): void;
  log(event: HostLogInput): void;
  cancelled(): boolean;
  device: { invoke(input: HostInvokeInput): Promise<HostInvokeResult> };
  prompt(input: HostPromptInput): Promise<HostPromptResult>;
  artifacts: { saveJson(input: HostSaveJsonInput): Promise<HostSaveJsonResult> };
}
export interface Plugin {
  describe(): PluginDescriptor;
  plan(input: PlanInput): Plan;
  execute(input: StepInput, host: Host): Promise<StepResult>;
}
