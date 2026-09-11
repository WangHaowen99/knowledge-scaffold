import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

/* 拒绝类错误必须给出可执行的下一步。
   现场代价：目标草稿分段报「包含未知字段：points」却不说这一段允许哪些字段，
   助手连续 22 次 400 一直在猜；同类的还有 #23 的计划目标标识、#32 的字段哈希。
   信息缺口越小，模型越倾向反复尝试而不是放弃，所以空提示比想象中贵。

   这条测试是棘轮：只允许变好，不允许变差。新增不带提示的抛错点会直接失败。 */

/** 逐个解析 new AgentCoreError(...) 的前三个参数，返回恢复提示为空的行号。 */
function emptyRecoverySites(source: string): number[] {
  const sites: number[] = [];
  const marker = 'new AgentCoreError(';
  for (let index = source.indexOf(marker); index >= 0; index = source.indexOf(marker, index + 1)) {
    const start = index + marker.length;
    let depth = 1;
    let cursor = start;
    while (cursor < source.length && depth > 0) {
      if (source[cursor] === '(') depth += 1;
      else if (source[cursor] === ')') depth -= 1;
      cursor += 1;
    }
    const body = source.slice(start, cursor - 1);
    const parts: string[] = [];
    let nesting = 0;
    let current = '';
    for (const character of body) {
      if ('([{'.includes(character)) nesting += 1;
      else if (')]}'.includes(character)) nesting -= 1;
      if (character === ',' && nesting === 0) { parts.push(current); current = ''; } else current += character;
    }
    parts.push(current);
    if (parts.length < 3) continue;
    if (!['[]', '[ ]'].includes(parts[2].trim())) continue;
    sites.push(source.slice(0, index).split('\n').length);
  }
  return sites;
}

test('拒绝类错误都带可执行的恢复提示，且不允许新增空提示', () => {
  const source = readFileSync('src/agent/core-service.ts', 'utf8');
  const sites = emptyRecoverySites(source);
  assert.deepEqual(
    sites,
    [],
    `以下行的 AgentCoreError 没有恢复提示，模型收到「不对」却不知道下一步：${sites.join('、')}。`
    + '\n请补一条可执行的提示：能穷举取值的列出取值，找不到资源的说明用哪个工具查，'
    + '冲突类说明先重新读取再提交。不要把错误消息重说一遍。',
  );
});

test('恢复提示不是把错误消息重说一遍', () => {
  // 提示的价值在于「下一步做什么」。整条提示与消息完全相同说明没提供新信息。
  const source = readFileSync('src/agent/core-service.ts', 'utf8');
  const duplicated = [...source.matchAll(/new AgentCoreError\(\s*'[A-Z_]+',\s*'([^']+)',\s*\['([^']+)'\]/g)]
    .filter((match) => match[1].trim() === match[2].trim())
    .map((match) => match[1]);
  assert.deepEqual(duplicated, [], `这些提示只是复述了错误消息：${duplicated.join('；')}`);
});

test('枚举类提示由实现变量生成，不手写平行清单', () => {
  /* 手写的取值清单会和实现漂移。现场教训：脚本补丁类型的提示里列了
     replace_symbol / insert_symbol / delete_symbol，而实现接受的是
     replace_function / replace_assignment / insert_before_function——
     三个全对不上，模型照着改只会继续失败，比不给提示更糟。
     所以「只支持 A、B、C」这种句子必须由变量拼出来，不能写死。 */
  const source = readFileSync('src/agent/core-service.ts', 'utf8');
  const offenders: string[] = [];
  source.split('\n').forEach((line, index) => {
    // 只看恢复提示所在的字符串字面量（单引号），模板串用反引号不在此列
    for (const match of line.matchAll(/'[^']*(?:只支持|只能是|从)\s*([A-Za-z_][A-Za-z0-9_]*(?:\s*[、,和或]\s*[A-Za-z_][A-Za-z0-9_]*)+)[^']*'/g)) {
      offenders.push(`第 ${index + 1} 行：${match[1]}`);
    }
  });
  assert.deepEqual(
    offenders,
    [],
    `这些提示手写了取值清单，请改成从判断用的同一个变量拼出来，例如 \`只支持 \${KINDS.join('、')}\`：\n${offenders.join('\n')}`,
  );
});

test('长任务防重入是一次拒绝，不能写成状态播报', () => {
  /* 现场教训：「该操作正在进行中，请稍候」被渲染在错误框里，读起来像进度条，
     用户对着它等了六分钟，而请求其实 33 毫秒就被拒了（见 #41）。 */
  const source = readFileSync('src/routes.ts', 'utf8');
  // 只看真正返回给用户的字符串，注释里引用旧文案不算（它在说明为什么要改）
  assert.doesNotMatch(source, /error: '该操作正在进行中，请稍候/);
  assert.match(source, /还没有结束，等它完成后再点这里/);
  // 必须说清楚「谁」还没结束，并给出起始时间让前端能算等待时长
  assert.match(source, /const OPERATION_LABELS: Record<string, string>/);
  assert.match(source, /runningOperation: current\?\.operation \|\| operation/);
  assert.match(source, /runningStartedAt: current\?\.startedAt \|\| ''/);
});

test('评测优化失败时把服务端附带的诊断摊开，不只取一句 message', () => {
  // 409 里带着 4 条可执行诊断，前端原来只取 cause.message，其余全丢。
  // 融合和 Studio 模块早就在读 error.data，这里是补齐落下的一块。
  const source = readFileSync('frontend/src/workbench/center/EvaluationView.jsx', 'utf8');
  assert.match(source, /function describeEvaluationFailure\(cause, fallback/);
  // 诊断面板和优化面板共用同一个解包函数，不留只取 cause.message 的分支
  assert.doesNotMatch(source, /catch \(cause\) \{\s*\n?\s*setError\(cause\.message\);\s*\n?\s*\} finally \{\s*\n?\s*setBusy\(''\); setLocalTask\(null\)/);
  assert.match(source, /cause\?\.data\?\.diagnoses/);
  assert.match(source, /item\.suggestedAction/);
  assert.match(source, /cause\?\.data\?\.runningStartedAt/);
  // propose/apply 都是同步长任务：运行态要在发请求前点亮（否则界面几分钟只有一个灰按钮），
  // 但必须在 finally 里落幕，失败路径不留假运行态
  for (const operation of ['propose', 'apply']) {
    const scope = source.slice(source.indexOf(`const ${operation} = async () => {`)).split('\n  };')[0];
    assert.match(scope, /try \{\s*\n\s*beginTask\('/);
    assert.match(scope, /finally \{ settleTask\(\); setBusy\(''\); \}/);
    assert.doesNotMatch(scope, /settleTask\(\);\s*\n\s*\} catch/);
  }
});

test('操作必须就读写哪些对象表态，两条创建路径不能各说各话', () => {
  /* upsert_operation 一直要求 related_objects 非空，但增量路径的 create_operation
     写成了 args.references ?? [] + allowEmpty，缺省即放行。助手于是只往 references
     里塞规则 ID，九个物理对象里六个成了孤儿，操作不知道该查哪张表（见 #46）。
     这里锁的是「必须表态」，不是「必须绑」——强制绑定只会把缺边换成查不出来的假边。 */
  const source = readFileSync('src/agent/core-service.ts', 'utf8');
  assert.match(source, /function operationReferences\(args: Record<string, unknown>\)/);
  assert.match(source, /OPERATION_OBJECT_BINDING_REQUIRED/);
  // 允许显式弃权，但必须写明理由
  assert.match(source, /args\.no_object_reason/);
  // 不能再退回「缺省即放行」
  assert.doesNotMatch(source, /domainReferenceRelations\(\s*'operation',\s*\n?\s*requiredStringArray\(args\.references \?\? \[\]/);

  const tools = readFileSync('modeling-agent/modeling_agent/tools.py', 'utf8');
  // 两条创建路径都要把 references 列进必填，且都提供弃权字段
  assert.equal((tools.match(/"no_object_reason": STRING,/g) || []).length, 2);
  assert.match(tools, /\["key", "title", "description", "flow", "references", "evidence"\]/);
  assert.match(tools, /"flow", "references", "evidence"\)/);
});
