import { existsSync, readFileSync, readdirSync, statSync } from 'node:fs';
import { dirname, extname, join, relative, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

const scriptDir = dirname(fileURLToPath(import.meta.url));
const projectDir = resolve(scriptDir, '..');
const sourceDir = join(projectDir, 'src');
const baselinePath = join(scriptDir, 'entry-reachability-baseline.json');
const MODULE_EXTENSIONS = ['.js', '.jsx', '.css'];
const TEST_FILE_PATTERN = /\.(?:test|spec)\.(?:js|jsx)$/;

const toProjectPath = (absolutePath) =>
  relative(projectDir, absolutePath).split(sep).join('/');

const walk = (directory) =>
  readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const absolutePath = join(directory, entry.name);
    if (entry.isDirectory()) {
      return walk(absolutePath);
    }
    if (!entry.isFile()) {
      return [];
    }
    if (
      !MODULE_EXTENSIONS.includes(extname(entry.name)) ||
      TEST_FILE_PATTERN.test(entry.name)
    ) {
      return [];
    }
    return [absolutePath];
  });

const extractRelativeImports = (source) => {
  const specifiers = new Set();
  const staticImport =
    /(?:import|export)\s+(?:[\w*\s{},]+?\s+from\s+)?['"](?<specifier>[^'"]+)['"]/g;
  const dynamicImport = /import\s*\(\s*['"](?<specifier>[^'"]+)['"]\s*\)/g;

  for (const pattern of [staticImport, dynamicImport]) {
    let match;
    while ((match = pattern.exec(source)) !== null) {
      if (match.groups.specifier.startsWith('.')) {
        specifiers.add(match.groups.specifier);
      }
    }
  }
  return [...specifiers];
};

const extractRelativeCssImports = (source) => {
  const specifiers = new Set();
  const uncommentedSource = source.replace(/\/\*[\s\S]*?\*\//g, '');
  const cssImport =
    /@import\s+(?:url\(\s*(?:(?<urlQuote>['"])(?<quotedUrl>.*?)\k<urlQuote>|(?<bareUrl>[^)'"\s]+))\s*\)|(?<quote>['"])(?<quoted>.*?)\k<quote>)/gi;
  let match;
  while ((match = cssImport.exec(uncommentedSource)) !== null) {
    const specifier =
      match.groups.quotedUrl ?? match.groups.bareUrl ?? match.groups.quoted;
    if (specifier.startsWith('.')) {
      specifiers.add(specifier);
    }
  }
  return [...specifiers];
};

const resolveImport = (importer, specifier) => {
  const isStylesheet = extname(importer) === '.css';
  const normalizedSpecifier = isStylesheet
    ? specifier.split(/[?#]/, 1)[0]
    : specifier;
  const extensions = isStylesheet ? ['.css'] : MODULE_EXTENSIONS;
  const basePath = resolve(dirname(importer), normalizedSpecifier);
  const candidates = [
    basePath,
    ...extensions.map((extension) => `${basePath}${extension}`),
    ...extensions.map((extension) => join(basePath, `index${extension}`)),
  ];
  return (
    candidates.find(
      (candidate) => existsSync(candidate) && statSync(candidate).isFile(),
    ) || null
  );
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
  const imports =
    extname(file) === '.css'
      ? extractRelativeCssImports(source)
      : extractRelativeImports(source);
  for (const specifier of imports) {
    const resolved = resolveImport(file, specifier);
    if (!resolved) {
      unresolvedImports.push({ importer: toProjectPath(file), specifier });
      continue;
    }
    if (productionFileSet.has(resolved)) {
      dependencies.push(resolved);
    }
  }
  graph.set(file, [...new Set(dependencies)].sort());
}

const reachable = new Set();
const visit = (file) => {
  if (reachable.has(file) || !graph.has(file)) {
    return;
  }
  reachable.add(file);
  graph.get(file).forEach(visit);
};
visit(entryPath);

const reachablePaths = productionFiles
  .filter((file) => reachable.has(file))
  .map(toProjectPath);
const unreachablePaths = productionFiles
  .filter((file) => !reachable.has(file))
  .map(toProjectPath);
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

if (
  !existsSync(entryPath) ||
  unapproved.length ||
  staleApprovals.length ||
  unresolvedImports.length
) {
  process.exitCode = 1;
}
