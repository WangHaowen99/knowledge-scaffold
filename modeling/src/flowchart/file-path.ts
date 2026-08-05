import type { FlowchartDocument } from './types.js';

const DIAGRAM_TITLE_MAX_LENGTH = 80;
const FORBIDDEN_FILE_CHARS = /[<>:"/\\|?*#&%\u0000-\u001f\u007f]/u;
const WINDOWS_RESERVED_NAME = /^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)/iu;
const LEGACY_DIAGRAM_PATH = /^diagrams\/process_\d+\.mmd$/u;

type DiagramIdentity = { id: string; title: string };

function normalizedTitle(value: string): string {
  return String(value ?? '').normalize('NFC').trim();
}

export function diagramFilePath(titleValue: string): string {
  const title = normalizedTitle(titleValue);
  if (
    !title
    || title.length > DIAGRAM_TITLE_MAX_LENGTH
    || title === '.'
    || title === '..'
    || title.endsWith('.')
    || FORBIDDEN_FILE_CHARS.test(title)
    || WINDOWS_RESERVED_NAME.test(title)
  ) {
    throw new Error(`流程名称“${title || String(titleValue ?? '')}”不能用于流程图文件名`);
  }
  return `diagrams/${title}.mmd`;
}

export function diagramFileEntries<T extends DiagramIdentity>(diagrams: T[]) {
  const seen = new Set<string>();
  return diagrams.map((diagram) => {
    const path = diagramFilePath(diagram.title);
    const identity = path.toLocaleLowerCase('en-US');
    if (seen.has(identity)) throw new Error(`流程名称重复：“${normalizedTitle(diagram.title)}”`);
    seen.add(identity);
    return { diagram, path, title: normalizedTitle(diagram.title) };
  });
}

export function obsoleteDiagramFilePaths(
  currentFiles: Record<string, string>,
  nextDocument: FlowchartDocument,
): string[] {
  const nextPaths = new Set(diagramFileEntries(nextDocument.diagrams).map((entry) => entry.path));
  const managedPaths = new Set(
    Object.keys(currentFiles).filter((path) => LEGACY_DIAGRAM_PATH.test(path)),
  );
  const previousSource = currentFiles['diagrams/processes.json'];
  if (previousSource) {
    try {
      const previous = JSON.parse(previousSource) as Partial<FlowchartDocument>;
      if (Array.isArray(previous.diagrams)) {
        for (const entry of diagramFileEntries(previous.diagrams)) {
          if (currentFiles[entry.path] !== undefined) managedPaths.add(entry.path);
        }
      }
    } catch {
      // 旧源文档不可解析时，只清理可明确识别的 process_N 历史派生文件。
    }
  }
  return [...managedPaths].filter((path) => !nextPaths.has(path)).sort();
}
