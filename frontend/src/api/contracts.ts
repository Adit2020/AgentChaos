import { z } from 'zod';

// Mirrors src/agentchaos/{models.py,api.py,artifacts/reader.py}.
// Types and runtime response validation share the same schemas.
const timestamp = z.string().refine((value) => Number.isFinite(Date.parse(value)));
const nullableVerdict = z.boolean().nullable();
const runStatus = z.enum(['running', 'succeeded', 'failed', 'cancelled']);
const jsonRecord = z.record(z.string(), z.json());
const count = z.number().int().nonnegative();

export const faultSchema = z.object({
  fault_type: z.string(),
  target_event_id: z.string().nullable(),
  target_tool: z.string().nullable(),
  occurrence: z.number().int().positive(),
  error_message: z.string().nullable(),
  parameters: jsonRecord,
});
const experimentSchema = z.object({
  experiment_id: z.string(),
  baseline_run_id: z.string(),
  faulted_run_id: z.string().nullable(),
  task: z.string().nullable(),
  fault: faultSchema,
});
export const eventSchema = z.object({
  event_id: z.string(),
  sequence: count,
  occurred_at: timestamp,
  kind: z.enum([
    'run.started',
    'run.completed',
    'run.failed',
    'run.cancelled',
    'model.request',
    'model.response',
    'model.error',
    'tool.call',
    'tool.result',
    'tool.error',
  ]),
  call_id: z.string().nullable(),
  name: z.string().nullable(),
  attributes: jsonRecord,
});
export const runSchema = z.object({
  run_id: z.string(),
  task: z.string(),
  adapter: z.string(),
  status: runStatus,
  started_at: timestamp,
  ended_at: timestamp.nullable(),
  events: z.array(eventSchema),
  output: z.json(),
});
export const comparisonSchema = z.object({
  experiment_id: z.string(),
  baseline_run_id: z.string(),
  faulted_run_id: z.string(),
  recovered: nullableVerdict,
  model_call_delta: z.number().int(),
  tool_call_delta: z.number().int(),
  duration_delta_ms: z.number().int().nullable(),
  baseline_model_calls: count,
  faulted_model_calls: count,
  baseline_tool_calls: count,
  faulted_tool_calls: count,
  baseline_injected_failures: count,
  faulted_injected_failures: count,
  fault_triggered: z.boolean(),
  baseline_duration_ms: count.nullable(),
  faulted_duration_ms: count.nullable(),
  baseline_task_success: nullableVerdict,
  faulted_task_success: nullableVerdict,
  continued_after_fault: z.boolean(),
  target_tool_retries: count,
  alternative_tool_calls: count,
});
const manifestBase = {
  schema_version: z.literal(1),
  export_id: z.string(),
  exported_at: timestamp,
  experiment: experimentSchema,
};
export const singleManifestSchema = z.object({
  ...manifestBase,
  artifact_type: z.literal('experiment'),
  baseline_status: runStatus,
  faulted_status: runStatus,
  fault_triggered: z.boolean(),
  recovered: nullableVerdict,
  files: z.array(z.string()),
});
const repetitionManifestSchema = z.object({
  number: z.number().int().positive(),
  state: z.enum(['recorded', 'incomplete', 'not_started']),
  directory: z.string().nullable(),
  experiment_id: z.string().nullable(),
  baseline_run_id: z.string().nullable(),
  faulted_run_id: z.string().nullable(),
  baseline_status: runStatus.nullable(),
  faulted_status: runStatus.nullable(),
  fault_triggered: nullableVerdict,
  recovered: nullableVerdict,
});
export const multiManifestSchema = z.object({
  ...manifestBase,
  artifact_type: z.literal('multi_run'),
  report_state: z.string(),
  summary_file: z.literal('summary.json'),
  repetitions: z.array(repetitionManifestSchema),
});
const manifestSchema = z.discriminatedUnion('artifact_type', [
  singleManifestSchema,
  multiManifestSchema,
]);
export const exportSummarySchema = z
  .object({
    export_id: z.string(),
    artifact_type: z.enum(['experiment', 'multi_run']),
    exported_at: timestamp,
    manifest: manifestSchema,
  })
  .refine(
    (entry) =>
      entry.artifact_type === entry.manifest.artifact_type &&
      entry.export_id === entry.manifest.export_id,
  );
export const exportListSchema = z.object({ exports: z.array(exportSummarySchema) });
const outcomeSchema = z.object({
  experiment: experimentSchema,
  baseline: runSchema,
  faulted: runSchema,
  comparison: comparisonSchema,
});
export const singleArtifactSchema = z.object({
  manifest: singleManifestSchema,
  baseline: runSchema,
  faulted: runSchema,
  comparison: comparisonSchema,
});
export const detailSchema = z
  .discriminatedUnion('artifact_type', [
    z.object({
      export_id: z.string(),
      artifact_type: z.literal('experiment'),
      artifact: singleArtifactSchema,
    }),
    z.object({
      export_id: z.string(),
      artifact_type: z.literal('multi_run'),
      artifact: z.object({
        manifest: multiManifestSchema,
        summary: jsonRecord,
        repetitions: z.array(
          z.object({
            number: z.number().int().positive(),
            state: z.enum(['recorded', 'incomplete', 'not_started']),
            outcome: outcomeSchema.nullable(),
          }),
        ),
      }),
    }),
  ])
  .refine((entry) => entry.export_id === entry.artifact.manifest.export_id);

export type RunEvent = z.infer<typeof eventSchema>;
export type AgentRun = z.infer<typeof runSchema>;
export type Comparison = z.infer<typeof comparisonSchema>;
export type ExportSummary = z.infer<typeof exportSummarySchema>;
export type SingleArtifact = z.infer<typeof singleArtifactSchema>;
export type ExportDetail = z.infer<typeof detailSchema>;
export type MultiArtifact = Extract<ExportDetail, { artifact_type: 'multi_run' }>['artifact'];

export const toolFaultTypeSchema = z.enum(['tool_error', 'tool_timeout', 'malformed_tool_output']);
export const scenarioSchema = z.object({
  scenario_id: z.string().min(1),
  name: z.string().min(1),
  description: z.string().min(1),
  supported_target_tools: z.array(z.string().min(1)).min(1),
  supported_fault_types: z.array(toolFaultTypeSchema).min(1),
  default_task: z.string().min(1),
  max_occurrence: z.number().int().min(1).max(10),
});
export const scenarioListSchema = z
  .object({ scenarios: z.array(scenarioSchema) })
  .refine(
    ({ scenarios }) => new Set(scenarios.map((s) => s.scenario_id)).size === scenarios.length,
  );
export const jobRequestSchema = z
  .object({
    scenario_id: z.string().min(1),
    fault_type: toolFaultTypeSchema,
    target_tool: z.string().min(1),
    occurrence: z.number().int().min(1).max(10),
    acknowledge_live_cost: z.literal(true),
  })
  .strict();
export const jobStatusSchema = z.object({
  job_id: z.string().min(1),
  scenario_id: z.string().min(1),
  state: z.enum(['queued', 'running', 'completed', 'failed', 'timed_out']),
  submitted_at: timestamp,
  started_at: timestamp.nullable(),
  finished_at: timestamp.nullable(),
  export_id: z.string().min(1).nullable(),
  partial_export: z.boolean(),
  cancellation_pending: z.boolean(),
  error: z.object({ code: z.string(), message: z.string() }).nullable(),
});
export type Scenario = z.infer<typeof scenarioSchema>;
export type JobRequest = z.infer<typeof jobRequestSchema>;
export type JobStatus = z.infer<typeof jobStatusSchema>;
