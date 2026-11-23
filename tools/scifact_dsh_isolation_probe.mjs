import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import { bootDsh } from './scifact_dsh_driver.mjs';

const { ctx, version, load } = await bootDsh();
const { defineTool } = await load('dsh-tools');
const { assembleContextFor } = await load('dsh-agent');

const prompt = 'Only the scoped public marker can be read.';
let executed = 0;
const first = await ctx.agents.create({
  sessionId: `scifact-isolation-${randomUUID()}`,
  setup(scope) {
    scope.systemPrompt.section({ name: 'probe', order: 0, complete: true, text: prompt });
    scope.tools.register(defineTool({
      name: 'read_virtual',
      description: 'Read one public probe marker.',
      parameters: { path: { type: 'string', required: true } },
      output: {
        schema: { type: 'string' },
        render: (_args, value) => [{ type: 'text', text: value }],
      },
      execute(args) {
        executed += 1;
        return args.path === 'public.txt' ? 'marker-read' : 'not-found';
      },
    }));
  },
});
try {
  const second = await ctx.agents.create({ sessionId: `scifact-control-${randomUUID()}` });
  try {
    const assembly = await ctx.systemPrompt.assemble(assembleContextFor(first.agent));
    const names = ctx.tools.schemas(first.agent).map((tool) => tool.name);
    assert.deepEqual(names, ['read_virtual']);
    assert.deepEqual(assembly.sections.map((section) => section.text), [prompt]);
    assert.deepEqual(ctx.tools.schemas(second.agent), []);
    const invoke = (agent, name, args) => ctx.tools.execute({
      callId: randomUUID(), name, arguments: args, agent, signal: new AbortController().signal,
    });
    assert.ok(JSON.stringify(await invoke(first.agent, 'read_virtual', { path: 'public.txt' })).includes('marker-read'));
    assert.ok(JSON.stringify(await invoke(first.agent, 'Bash', {})).includes('UNKNOWN_TOOL'));
    assert.ok(JSON.stringify(await invoke(second.agent, 'read_virtual', { path: 'public.txt' })).includes('UNKNOWN_TOOL'));
    assert.ok(JSON.stringify(await invoke(first.agent, 'read_virtual', { path: 5 })).includes('INVALID_ARGS'));
    assert.equal(executed, 1);
    assert.deepEqual(ctx.llm.listProviders(), []);
    process.stdout.write(JSON.stringify({
      dsh_version: version,
      prompt_preserved: true,
      agent_visible_tools: names,
      second_agent_visible_tools: [],
      valid_tool_executions: executed,
      shell_denied: true,
      cross_agent_denied: true,
      invalid_arguments_denied: true,
      provider_routes: 0,
      model_calls: 0,
    }) + '\n');
  } finally {
    await second.dispose();
  }
} finally {
  await first.dispose();
}
