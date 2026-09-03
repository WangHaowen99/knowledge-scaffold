import type { SpecScript } from '../types.js';

type ScriptContract = Omit<SpecScript, 'sourceCode'>;

const CORE_SCAFFOLD_MARKER = '# modeling-core-python-scaffold: v1';

export function isCoreManagedPythonScaffold(sourceCode: string): boolean {
  return sourceCode.startsWith(`${CORE_SCAFFOLD_MARKER}\n`);
}

function pythonString(value: string): string {
  return JSON.stringify(value);
}

function pythonLiteral(value: string | number | boolean): string {
  if (typeof value === 'boolean') return value ? 'True' : 'False';
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) throw new Error('脚本参数默认值必须是有限数字');
    return String(value);
  }
  return pythonString(value);
}

function pythonType(parameterType: string): string | undefined {
  switch (parameterType.trim().toLowerCase()) {
    case 'integer':
    case 'int':
      return 'int';
    case 'number':
    case 'float':
    case 'double':
    case 'decimal':
      return 'float';
    case 'boolean':
    case 'bool':
      return 'parse_boolean';
    case 'object':
    case 'array':
    case 'json':
      return 'json.loads';
    case 'string':
    case 'str':
      return 'str';
    default:
      return undefined;
  }
}

function argumentLine(parameter: ScriptContract['parameters'][number]): string {
  const options = [pythonString(parameter.flag), `dest=${pythonString(parameter.name)}`];
  if (parameter.action) {
    options.push(`action=${pythonString(parameter.action)}`);
  } else {
    const type = pythonType(parameter.type);
    if (type) options.push(`type=${type}`);
    if (parameter.required) options.push('required=True');
    if (parameter.multiple) options.push('nargs="+"');
  }
  if (parameter.defaultValue !== undefined) {
    options.push(`default=${pythonLiteral(parameter.defaultValue)}`);
  }
  if (parameter.description) options.push(`help=${pythonString(parameter.description)}`);
  return `    parser.add_argument(${options.join(', ')})`;
}

export function buildPythonScriptScaffold(contract: ScriptContract): string {
  const hasExplicitBoolean = contract.parameters.some((parameter) => (
    !parameter.action && ['bool', 'boolean'].includes(parameter.type.trim().toLowerCase())
  ));
  const imports = [
    CORE_SCAFFOLD_MARKER,
    'import argparse',
    'import json',
  ];
  const blocks: string[] = [imports.join('\n')];
  blocks.push([
    'class DataProviderClient:',
    '    def __new__(cls, *args, **kwargs):',
    '        from shared_capabilities.ontology_script_sdk import DataProviderClient as _DataProviderClient',
    '        return _DataProviderClient(*args, **kwargs)',
  ].join('\n'));
  if (hasExplicitBoolean) {
    blocks.push([
      'def parse_boolean(value):',
      '    if isinstance(value, bool):',
      '        return value',
      '    normalized = str(value).strip().lower()',
      '    if normalized in {"1", "true", "yes", "on"}:',
      '        return True',
      '    if normalized in {"0", "false", "no", "off"}:',
      '        return False',
      '    raise argparse.ArgumentTypeError("expected true or false")',
    ].join('\n'));
  }
  const outputKeys = contract.outputs.map((output) => pythonString(output.key));
  blocks.push(`REQUIRED_OUTPUT_KEYS = (${outputKeys.join(', ')}${outputKeys.length === 1 ? ',' : ''})`);
  blocks.push([
    'def run_business_logic(inputs):',
    '    raise NotImplementedError("run_business_logic must be implemented")',
  ].join('\n'));
  const main = [
    'def main(argv=None):',
    `    parser = argparse.ArgumentParser(description=${pythonString(contract.description)})`,
    ...contract.parameters.map(argumentLine),
    '    args = parser.parse_args(argv)',
    '    inputs = {',
    ...contract.parameters.map((parameter) => (
      `        ${pythonString(parameter.name)}: getattr(args, ${pythonString(parameter.name)}),`
    )),
    '    }',
    '    result = run_business_logic(inputs)',
    '    if not isinstance(result, dict):',
    '        raise TypeError("run_business_logic must return a dict")',
    '    if "error" not in result:',
    '        missing = [key for key in REQUIRED_OUTPUT_KEYS if key not in result]',
    '        if missing:',
    '            raise ValueError("missing output fields: " + ", ".join(missing))',
    '    print(json.dumps(result, ensure_ascii=False))',
  ];
  blocks.push(main.join('\n'));
  blocks.push([
    'if __name__ == "__main__":',
    '    main()',
  ].join('\n'));
  return `${blocks.join('\n\n')}\n`;
}
