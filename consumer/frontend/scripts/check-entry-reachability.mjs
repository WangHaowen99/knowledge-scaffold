import { existsSync, readFileSync, readdirSync, statSync } from 'node:fs';
import { dirname, extname, join, relative, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

const scriptDir = dirname(fileURLToPath(import.meta.url));
const projectDir = resolve(scriptDir, '..');
const sourceDir = join(projectDir, 'src');
const baselinePath = join(scriptDir, 'entry-reachability-baseline.json');
const CODE_EXTENSIONS = ['.js', '.jsx'];
const TEST_FILE_PATTERN = /\.(?:test|spec)\.(?:js|jsx)$/;

const toProjectPath = (absolutePath) => relative(projectDir, absolutePath).split(sep).join('/');

const walk = (directory) => readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
  const absolutePath = join(directory, entry.name);
  if (entry.isDirectory()) return walk(absolutePath);
  if (!entry.isFile()) return [];
  if (!CODE_EXTENSIONS.includes(extname(entry.name)) || TEST_FILE_PATTERN.test(entry.name)) return [];
  return [absolutePath];
});

const extractRelativeImports = (source) => {
  const specifiers = new Set();
  const staticImport = /(?:import|export)\s+(?:[\w*\s{},]+?\s+from\s+)?['"]([^'"]+)['"]/g;
  const dynamicImport = /import\s*\(\s*['"]([^'"]+)['"]\s*\)/g;

  for (const pattern of [staticImport, dynamicImport]) {
    let match;
    while ((match = pattern.exec(source)) !== null) {
      if (match[1].startsWith('.')) specifiers.add(match[1]);
    }
  }
  return [...specifiers];
};

const resolveImport = (importer, specifier) => {
  const basePath = resolve(dirname(importer), specifier);
  const candidates = [
    basePath,
    ...CODE_EXTENSIONS.map((extension) => `${basePath}${extension}`),
    ...CODE_EXTENSIONS.map((extension) => join(basePath, `index${extension}`)),
  ];
  return candidates.find((candidate) => existsSync(candidate) && statSync(candidate).isFile()) || null;
};

const baseline = JSON.parse(readFileSync(baselinePath, 'utf8'));
const entryPath = join(projectDir, baseline.entry);
const productionFiles = walk(sourceDir).sort();
const productionFileSet = new Set(productionFiles);
const graph = new Map();
const unresolvedImports = [];

for (const file of productionFiles) {
  const dependencies = [];
  const source = readFileSync(file, 'utf8');
  for (const specifier of extractRelativeImports(source)) {
    const resolved = resolveImport(file, specifier);
    if (!resolved) {
      unresolvedImports.push({ importer: toProjectPath(file), specifier });
      continue;
    }
    if (productionFileSet.has(resolved)) dependencies.push(resolved);
  }
  graph.set(file, [...new Set(dependencies)].sort());
}

const reachable = new Set();
const visit = (file) => {
  if (reachable.has(file) || !graph.has(file)) return;
  reachable.add(file);
  graph.get(file).forEach(visit);
};
visit(entryPath);

const reachablePaths = productionFiles.filter((file) => reachable.has(file)).map(toProjectPath);
const unreachablePaths = productionFiles.filter((file) => !reachable.has(file)).map(toProjectPath);
const approved = [...new Set(baseline.approvedUnreachable || [])].sort();
const approvedSet = new Set(approved);
const unreachableSet = new Set(unreachablePaths);
const unapproved = unreachablePaths.filter((file) => !approvedSet.has(file));
const staleApprovals = approved.filter((file) => !unreachableSet.has(file));
const dependencyGraph = Object.fromEntries(
  [...graph.entries()].map(([file, dependencies]) => [
    toProjectPath(file),
    dependencies.map(toProjectPath),
  ]),
);

const report = {
  entry: baseline.entry,
  productionModules: productionFiles.length,
  reachable: reachablePaths,
  approvedUnreachable: approved,
  unapprovedUnreachable: unapproved,
  staleApprovals,
  unresolvedImports,
  dependencyGraph,
};

if (process.argv.includes('--json')) {
  console.log(JSON.stringify(report, null, 2));
} else {
  console.log(`Entry: ${report.entry}`);
  console.log(`Production modules: ${report.productionModules}`);
  console.log(`Reachable: ${report.reachable.length}`);
  console.log(`Approved unreachable: ${report.approvedUnreachable.length}`);
  if (unapproved.length) {
    console.error('Unapproved unreachable modules:');
    unapproved.forEach((file) => console.error(`  - ${file}`));
  }
  if (staleApprovals.length) {
    console.error('Stale unreachable approvals:');
    staleApprovals.forEach((file) => console.error(`  - ${file}`));
  }
  if (unresolvedImports.length) {
    console.error('Unresolved relative imports:');
    unresolvedImports.forEach(({ importer, specifier }) => {
      console.error(`  - ${importer}: ${specifier}`);
    });
  }
}

if (!existsSync(entryPath) || unapproved.length || staleApprovals.length || unresolvedImports.length) {
  process.exitCode = 1;
}
