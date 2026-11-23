import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';
import { createRequire } from 'node:module';
import { join } from 'node:path';
import { createInterface } from 'node:readline';
import { setTimeout } from 'node:timers/promises';
import { fileURLToPath, pathToFileURL } from 'node:url';

export async function bootDsh() {
  const manifest = join(process.env.APPDATA, 'npm', 'node_modules', '@deepseek-ai', 'dsh', 'package.json');
  const resolve = createRequire(manifest);
  const version = resolve(manifest).version;
  assert.equal(version, '0.1.0-rc.6');
  const load = (name) => import(pathToFileURL(resolve.resolve(`@deepseek-ai/${name}`)).href);
  const { Context } = await load('cordis');
  const ctx = new Context();
  const packages = ['dsh-agent', 'dsh-session', 'dsh-llm', 'dsh-system-prompt', 'dsh-tools', 'dsh-agent-loop'];
  for (const name of packages) {
    ctx.plugin((await load(name)).default, name === 'dsh-agent-loop' ? { agents: [], maxParallelToolCalls: 1 } : {});
  }
  const end = Date.now() + 2_000;
  while (!ctx.get('agentLoop') && Date.now() < end) await setTimeout(5);
  assert.ok(ctx.get('agentLoop'), 'agent loop unavailable');
  return { ctx, version, load };
}

async function readLine(lines) {
  const line = await lines.next();
  assert.ok(!line.done && line.value.length <= 1_000_000, 'missing or oversized bridge input');
  return JSON.parse(line.value);
}

const allowedTools = new Set(['search_literature', 'read_literature', 'Read', 'Grep', 'Glob', 'read_virtual']);
const model = 'gpt-6-luna';
const provider = 'authorized-parent';
const emit = (row) => process.stdout.write(`${JSON.stringify(row)}\n`);

async function run() {
  const receiver = createInterface({ input: process.stdin, crlfDelay: Infinity, terminal: false });
  try {
    const lines = receiver[Symbol.asyncIterator]();
    const request = await readLine(lines);
    assert.ok(typeof request.system === 'string' && typeof request.prompt === 'string');
    const specs = request.tools;
    assert.ok(Array.isArray(specs) && specs.length > 0 && specs.every((tool) => allowedTools.has(tool.name)));
    const { ctx, version, load } = await bootDsh();
    const { LlmAdapter, createUserMessage } = await load('dsh-llm');
    let seq = 0;
    let waiting = false;
    let answer = null;
    let calledTools = 0;
    async function rpc(kind, payload) {
      assert.ok(!waiting, 'overlapping bridge request');
      waiting = true;
      try {
        const id = ++seq;
        emit({ id, kind, payload });
        const response = await readLine(lines);
        assert.ok(response.id === id && response.ok === true, 'bridge response rejected');
        return response.result;
      } finally {
        waiting = false;
      }
    }
    class ParentTransport extends LlmAdapter {
      async *stream(options) {
        options.signal?.throwIfAborted();
        assert.equal(options.provider, provider);
        assert.equal(options.model, model);
        assert.equal(options.reasoningEffort, undefined);
        assert.equal(options.purpose, undefined);
        const { blocks, finish } = await rpc('model', {
          system: options.system,
          model: options.model,
          tools: options.tools,
          messages: options.messages.map(({ role, content }) => ({ role, content })),
        });
        options.signal?.throwIfAborted();
        assert.ok(Array.isArray(blocks) && ['stop', 'tool-calls'].includes(finish));
        for (const [index, block] of blocks.entries()) {
          yield { type: 'block-start', index, blockType: block.type };
          if (block.type === 'text') {
            yield { type: 'text-delta', index, text: block.text };
          } else {
            assert.equal(block.type, 'tool-call');
            yield { type: 'tool-call-delta', index, id: block.id, name: block.name, argumentsDelta: block.arguments };
          }
          yield { type: 'block-end', index, block };
        }
        if (finish === 'stop') answer = blocks.filter((block) => block.type === 'text').map((block) => block.text).join('');
        yield { type: 'finish', reason: { kind: finish } };
      }
    }
    ctx.llm.registerAdapter([provider], new ParentTransport());
    const handle = await ctx.agents.create({
      sessionId: `scifact-dsh-${randomUUID()}`,
      agentOptions: { provider, model },
      setup(scope) {
        scope.systemPrompt.section({ name: 'parent', order: 0, complete: true, text: request.system });
        for (const spec of specs) {
          scope.tools.register({
            ...spec,
            output: { schema: { type: 'string' }, render: (_args, value) => [{ type: 'text', text: value }] },
            async execute(args, call) {
              call.signal.throwIfAborted();
              const result = await rpc('tool', { name: spec.name, args, callId: call.callId });
              call.signal.throwIfAborted();
              assert.equal(typeof result, 'string');
              calledTools += 1;
              return result;
            },
          });
        }
      },
    });
    try {
      const { agent } = handle;
      let prompt = request.prompt;
      while (true) {
        answer = null;
        agent.followup(createUserMessage({ source: { kind: 'user' }, content: [{ type: 'text', text: prompt }] }));
        await agent.whenIdle();
        const ending = agent.session.events.findLast((e) => e.type === 'turn/end');
        emit({ kind: 'done', version, sessionId: agent.id, answer, calledTools, turnEnd: ending?.data.reason.kind });
        const control = await readLine(lines);
        if (control.kind === 'close') break;
        assert.ok(control.kind === 'followup' && typeof control.prompt === 'string');
        prompt = control.prompt;
      }
    } finally {
      await handle.dispose();
    }
  } finally {
    receiver.close();
    process.stdin.pause();
  }
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  try {
    await run();
  } catch (error) {
    emit({ kind: 'failure', error: error.name });
    process.exitCode = 2;
  }
}
