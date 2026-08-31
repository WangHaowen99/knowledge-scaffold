import assert from 'node:assert/strict';
import test from 'node:test';

import {
  validateGeneratedScriptSafety,
  validateGeneratedScriptSecurity,
} from '../src/workflow/script-safety.js';
import { renderPackage } from '../src/workflow/package-render.js';
import { runFormatChecks } from '../src/workflow/package-check.js';
import { validateSpecIntegrity } from '../src/workflow/spec-validation.js';
import type { PackageSpec, SpecScript } from '../src/types.js';

function script(sourceCode: string): SpecScript {
  return {
    key: 'calculate_plan',
    title: '计算生产计划',
    description: '读取结构化业务数据并计算生产计划。',
    boundOperation: 'analyze_plan',
    runtime: 'python',
    entrypoint: 'calculate_plan.py',
    timeoutSeconds: 30,
    parameters: [{
      name: 'input_json', flag: '--input-json', type: 'string', required: true,
      description: '结构化计划 JSON',
    }],
    outputs: [{ key: 'result', type: 'object', description: '计算结果' }],
    boundaries: ['不得内置评测答案。'],
    sourceCode,
  };
}

test('generated scripts allow non-builtin module methods such as re.compile', () => {
  assert.doesNotThrow(() => validateGeneratedScriptSafety(script(`
import argparse
import json
import re

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-json", required=True)
    args = parser.parse_args()
    pattern = re.compile(r"^plan_[0-9]+$")
    spaced_pattern = re . compile(r"^tank_[0-9]+$")
    print(json.dumps({
        "result": bool(pattern.match(json.loads(args.input_json)["plan"]))
            or bool(spaced_pattern.match("tank_1"))
    }))

if __name__ == "__main__":
    main()
`)));
});

test('generated scripts still reject direct dynamic execution builtins', () => {
  for (const sourceCode of [
    'eval("1 + 1")',
    'exec("result = 2")',
    'compile("result = 2", "<string>", "exec")',
    '__import__("os")',
    'import builtins\nbuiltins.compile("result = 2", "<string>", "exec")',
  ]) {
    assert.throws(
      () => validateGeneratedScriptSecurity(script(sourceCode)),
      /脚本安全检查失败.*动态执行/,
    );
  }
});

test('generated scripts reject unresolved runtime data stubs', () => {
  assert.throws(() => validateGeneratedScriptSafety(script(`
import argparse
import json

def query_table(name):
    raise NotImplementedError("provided by runtime")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-json", required=True)
    args = parser.parse_args()
    print(json.dumps({"rows": query_table(args.input_json)}))

if __name__ == "__main__":
    main()
`)), /占位数据接口|NotImplementedError/);
});

test('generated scripts require an argparse CLI entrypoint', () => {
  assert.throws(() => validateGeneratedScriptSafety(script(`
import json

def main(payload):
    return {"result": payload}
`)), /argparse|CLI/);
});

test('boolean action must match the contract and argparse declaration', () => {
  const source = (argument: string) => `
import argparse
import json

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-json", required=True)
    ${argument}
    args = parser.parse_args()
    print(json.dumps({"result": {"enabled": args.enabled, "input": args.input_json}}))

if __name__ == "__main__":
    main()
`;
  const withBoolean = (parameter: SpecScript['parameters'][number], sourceCode: string): SpecScript => ({
    ...script(sourceCode),
    parameters: [script(sourceCode).parameters[0], parameter],
  });
  const storeTrue = {
    name: 'enabled', flag: '--enabled', type: 'boolean', required: false,
    defaultValue: false, action: 'store_true' as const,
  };

  assert.doesNotThrow(() => validateGeneratedScriptSafety(withBoolean(
    storeTrue,
    source('parser.add_argument("--enabled", action="store_true")'),
  )));
  assert.throws(() => validateGeneratedScriptSafety(withBoolean(
    { ...storeTrue, action: undefined },
    source('parser.add_argument("--enabled", action="store_true")'),
  )), /普通 boolean 契约.*store_true/u);
  assert.throws(() => validateGeneratedScriptSafety(withBoolean(
    storeTrue,
    source('parser.add_argument("--enabled", type=str)'),
  )), /源码 action.*store_true.*不一致/u);
  assert.throws(() => validateGeneratedScriptSafety(withBoolean(
    { ...storeTrue, defaultValue: true },
    source('parser.add_argument("--enabled", action="store_true")'),
  )), /默认值.*store_true.*不一致/u);
});

test('generated scripts reject invalid Python syntax before creating a candidate revision', () => {
  assert.throws(() => validateGeneratedScriptSafety(script(`
import argparse
import json

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-json", required=True)
    args = parser.parse_args()
    amount = json.loads(args.input_json)["amount"] * <运行时数值>
    print(json.dumps({"result": amount}))

if __name__ == "__main__":
    main()
`)), /Python.*语法|语法检查/);
});

test('generated scripts accept the supported provider SDK and structured JSON output', () => {
  assert.doesNotThrow(() => validateGeneratedScriptSafety(script(`
import argparse
import json
from shared_capabilities.ontology_script_sdk import DataProviderClient

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-json", required=True)
    args = parser.parse_args()
    with DataProviderClient() as client:
        response = client.query("读取生产计划基础数据")
        if not response.success:
            raise RuntimeError(response.error or response.message)
    print(json.dumps({"result": response.data, "input": json.loads(args.input_json)}, ensure_ascii=False))

if __name__ == "__main__":
    main()
`)));
});

test('generated scripts reject unsupported keyword filters on provider SDK queries', () => {
  assert.throws(() => validateGeneratedScriptSafety(script(`
import argparse
import json
from shared_capabilities.ontology_script_sdk import DataProviderClient

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-json", required=True)
    args = parser.parse_args()
    with DataProviderClient() as client:
        rows = client.query("orders", filters={"status": "open"})
    print(json.dumps({"rows": rows, "input": json.loads(args.input_json)}, ensure_ascii=False))

if __name__ == "__main__":
    main()
`)), /query\(question\)|filters|ProviderResult/);
});

test('generated scripts must unwrap and validate provider results before using rows', () => {
  assert.throws(() => validateGeneratedScriptSafety(script(`
import argparse
import json
from shared_capabilities.ontology_script_sdk import DataProviderClient

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-json", required=True)
    args = parser.parse_args()
    with DataProviderClient() as client:
        rows = client.query("读取订单数据")
    print(json.dumps({"count": len(rows), "input": json.loads(args.input_json)}, ensure_ascii=False))

if __name__ == "__main__":
    main()
`)), /ProviderResult|success|data/);
});

test('read-only package projection can render legacy invalid scripts for diagnosis', () => {
  const legacyScript = script(`
import argparse
import json
from shared_capabilities.ontology_script_sdk import DataProviderClient

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-json", required=True)
    args = parser.parse_args()
    with DataProviderClient() as client:
        rows = client.query("orders", filters={"status": "open"})
    print(json.dumps({"rows": rows, "input": json.loads(args.input_json)}, ensure_ascii=False))

if __name__ == "__main__":
    main()
`);
  const spec: PackageSpec = {
    id: 'legacy-package', name: '存量知识包', description: '用于诊断修复',
    version: '1.0.0', language: 'zh-CN', defaultProviderAlias: 'data',
    objects: [], rules: [], metrics: [], scripts: [legacyScript],
    operations: [{
      key: 'analyze_plan', title: '分析计划', description: '分析计划',
      inputs: [], outputs: [], flow: ['执行确定性脚本'], relatedObjects: [],
      script: { key: legacyScript.key, required: true },
    }],
  };

  assert.throws(() => renderPackage(spec), /脚本执行契约检查失败/);
  const files = renderPackage(spec, { validateScripts: false });
  assert.match(files['scripts/calculate_plan.py'], /filters=/);
  const findings = runFormatChecks(spec, files);
  assert.ok(findings.some((finding) => (
    finding.severity === '错误'
    && finding.category === '脚本执行契约'
    && finding.target === legacyScript.key
    && /ProviderResult|filters/.test(finding.message)
    && /修正/.test(finding.suggestion ?? '')
  )));
});

test('knowledge packages reject scripts that share the same entrypoint', () => {
  const first = script(`
import argparse
import json

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-json", required=True)
    args = parser.parse_args()
    print(json.dumps({"result": json.loads(args.input_json)}))

if __name__ == "__main__":
    main()
`);
  const second: SpecScript = {
    ...first,
    key: 'validate_tank_level',
    title: '校验储罐液位',
    boundOperation: 'validate_tank_level',
    entrypoint: 'Calculate_Plan.py',
  };
  const spec: PackageSpec = {
    id: 'duplicate-entrypoint-package', name: '重复入口知识包', description: '用于校验重复脚本入口',
    version: '1.0.0', language: 'zh-CN', defaultProviderAlias: 'data',
    objects: [], rules: [], metrics: [], scripts: [first, second],
    operations: [{
      key: 'analyze_plan', title: '分析计划', description: '分析计划',
      inputs: [], outputs: [], flow: ['执行确定性脚本'], relatedObjects: [],
      script: { key: first.key, required: true },
    }, {
      key: 'validate_tank_level', title: '校验储罐液位', description: '校验储罐液位',
      inputs: [], outputs: [], flow: ['执行确定性脚本'], relatedObjects: [],
      script: { key: second.key, required: true },
    }],
  };

  assert.throws(() => validateSpecIntegrity(spec), /脚本入口文件冲突.*calculate_plan\.py/);

  const files = renderPackage(spec, { validateScripts: false });
  const findings = runFormatChecks(spec, files).filter((finding) => finding.category === '脚本入口冲突');
  assert.equal(findings.length, 2);
  assert.deepEqual(findings.map((finding) => finding.target).sort(), [
    'script:calculate_plan',
    'script:validate_tank_level',
  ]);
  assert.ok(findings.every((finding) => (
    finding.severity === '错误'
    && finding.releaseBlocking === true
    && /calculate_plan\.py/i.test(finding.message)
    && /唯一/.test(finding.suggestion ?? '')
  )));
});
