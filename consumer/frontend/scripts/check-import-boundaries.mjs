import { existsSync, readFileSync, readdirSync, statSync } from 'node:fs';
import { dirname, extname, isAbsolute, join, relative, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

const scriptDir = dirname(fileURLToPath(import.meta.url));
const projectDir = resolve(scriptDir, '..');
const sourceDir = join(projectDir, 'src');
const publicDir = join(projectDir, 'public');
const MODULE_EXTENSIONS = ['.js', '.jsx', '.css'];
const CODE_EXTENSIONS = new Set(['.js', '.jsx']);
const SOURCE_EXTENSIONS = new Set(MODULE_EXTENSIONS);
const PROHIBITED_DIRECTORY_PATTERN = /(^|\/)(?:redesign|legacy|old|v1)(?:\/|$)/;

const toSourcePath = (absolutePath) => relative(sourceDir, absolutePath).split(sep).join('/');

const walkFiles = (directory) => readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
  const absolutePath = join(directory, entry.name);
  if (entry.isDirectory()) return walkFiles(absolutePath);
  if (!entry.isFile()) return [];
  return [absolutePath];
});

const extractImports = (source) => {
  const specifiers = new Set();
  const staticImport = /(?:import|export)\s+(?:[\w*\s{},]+?\s+from\s+)?['"]([^'"]+)['"]/g;
  const dynamicImport = /import\s*\(\s*['"]([^'"]+)['"]\s*\)/g;
  for (const pattern of [staticImport, dynamicImport]) {
    let match;
    while ((match = pattern.exec(source)) !== null) specifiers.add(match[1]);
  }
  return [...specifiers];
};

const extractCssImports = (source) => {
  const specifiers = new Set();
  const uncommentedSource = source.replace(/\/\*[\s\S]*?\*\//g, '');
  const cssImport = /@import\s+(?:url\(\s*(?:(['"])(.*?)\1|([^)'"\s]+))\s*\)|(['"])(.*?)\4)/gi;
  let match;
  while ((match = cssImport.exec(uncommentedSource)) !== null) {
    specifiers.add(match[2] ?? match[3] ?? match[5]);
  }
  return [...specifiers];
};

const resolveImport = (importer, specifier) => {
  if (!specifier.startsWith('.')) return null;
  const basePath = resolve(dirname(importer), specifier);
  const candidates = [
    basePath,
    ...MODULE_EXTENSIONS.map((extension) => `${basePath}${extension}`),
    ...MODULE_EXTENSIONS.map((extension) => join(basePath, `index${extension}`)),
  ];
  return candidates.find((candidate) => existsSync(candidate) && statSync(candidate).isFile()) || null;
};

const resolveCssImport = (importer, specifier) => {
  if (!specifier.startsWith('.')) return null;

  const pathSpecifier = specifier.split(/[?#]/, 1)[0];
  const basePath = resolve(dirname(importer), pathSpecifier);
  const extension = extname(basePath);
  const candidates = extension === '.css'
    ? [basePath]
    : extension
      ? []
      : [`${basePath}.css`, join(basePath, 'index.css')];

  return candidates.find((candidate) => existsSync(candidate) && statSync(candidate).isFile()) || null;
};

const isWithinSource = (absolutePath) => {
  const sourcePath = relative(sourceDir, absolutePath);
  return sourcePath !== '' && !sourcePath.startsWith(`..${sep}`) && !isAbsolute(sourcePath);
};

const describeModule = (absolutePath) => {
  const sourcePath = toSourcePath(absolutePath);
  const parts = sourcePath.split('/');
  return {
    absolutePath,
    sourcePath,
    layer: parts[0],
    feature: parts[0] === 'features' ? parts[1] : null,
  };
};

const sourceFiles = walkFiles(sourceDir).sort();
const modules = sourceFiles.filter((file) => SOURCE_EXTENSIONS.has(extname(file))).map(describeModule);
const violations = [];
const report = (module, message) => violations.push(`${module.sourcePath}: ${message}`);

for (const file of sourceFiles) {
  const sourcePath = toSourcePath(file);
  if (PROHIBITED_DIRECTORY_PATTERN.test(sourcePath)) {
    violations.push(`${sourcePath}: production source cannot live in a historical directory`);
  }
  if (!sourcePath.includes('/') && sourcePath !== 'main.jsx') {
    violations.push(`${sourcePath}: root-level production source is forbidden; use app/features/shared`);
  }
}

if (existsSync(publicDir)) {
  for (const file of walkFiles(publicDir)) {
    const publicPath = relative(projectDir, file).split(sep).join('/');
    if (PROHIBITED_DIRECTORY_PATTERN.test(publicPath)) {
      violations.push(`${publicPath}: public assets cannot live in a historical directory`);
    }
  }
}

const validateLayerDependency = (module, target, specifier, { cssImport = false } = {}) => {
  if (module.layer === 'features' && target.layer === 'app') {
    report(module, `feature cannot depend on app (${specifier})`);
  }
  if (module.layer === 'shared' && (target.layer === 'app' || target.layer === 'features')) {
    report(module, `shared cannot depend on ${target.layer} (${specifier})`);
  }
  if (module.layer === 'shared' && module.sourcePath.startsWith('shared/ui/') && target.sourcePath.startsWith('shared/api/')) {
    report(module, `shared/ui cannot depend on shared/api (${specifier})`);
  }
  if (module.layer === 'features' && target.layer === 'features' && module.feature !== target.feature) {
    if (cssImport) {
      report(module, `cross-feature CSS imports are forbidden; keep styles inside ${module.feature} (${specifier})`);
      return;
    }

    const expectedJs = `features/${target.feature}/index.js`;
    const expectedJsx = `features/${target.feature}/index.jsx`;
    if (target.sourcePath !== expectedJs && target.sourcePath !== expectedJsx) {
      report(module, `cross-feature dependency must use ${target.feature}'s public index (${specifier})`);
    }
  }
};

for (const module of modules) {
  const source = readFileSync(module.absolutePath, 'utf8');
  const extension = extname(module.absolutePath);
  const isCodeModule = CODE_EXTENSIONS.has(extension);
  const isStylesheet = extension === '.css';

  if (isCodeModule && module.sourcePath !== 'main.jsx' && /\bcreateRoot\s*\(/.test(source)) {
    report(module, 'only main.jsx may create the React root');
  }
  if (isCodeModule && module.layer === 'shared' && module.sourcePath.startsWith('shared/lib/')) {
    const reactImport = extractImports(source).find((specifier) => specifier === 'react' || specifier.startsWith('react/'));
    if (reactImport) report(module, `shared/lib must stay React-free (imports ${reactImport})`);
  }
  if (isCodeModule && module.layer === 'shared' && module.sourcePath.startsWith('shared/ui/') && /\bfetch\s*\(/.test(source)) {
    report(module, 'shared/ui cannot call business APIs directly');
  }

  const imports = isCodeModule ? extractImports(source) : [];
  if (isCodeModule && module.sourcePath !== 'main.jsx' && imports.includes('react-dom/client')) {
    report(module, 'only main.jsx may import react-dom/client');
  }
  if (isCodeModule && module.sourcePath === 'main.jsx') {
    const relativeImports = imports.filter((specifier) => specifier.startsWith('.')).sort();
    const allowedImports = ['./app/App.jsx', './shared/styles/base.css'];
    if (JSON.stringify(relativeImports) !== JSON.stringify(allowedImports)) {
      report(module, `must only mount app/App and global base styles (found ${relativeImports.join(', ')})`);
    }
  }

  for (const specifier of imports) {
    const targetPath = resolveImport(module.absolutePath, specifier);
    if (!targetPath) continue;
    const target = describeModule(targetPath);

    validateLayerDependency(module, target, specifier);
  }

  if (isStylesheet) {
    for (const specifier of extractCssImports(source)) {
      if (!specifier.startsWith('.')) continue;

      const targetPath = resolveCssImport(module.absolutePath, specifier);
      if (!targetPath) {
        report(module, `relative CSS @import cannot be resolved (${specifier})`);
        continue;
      }
      if (!isWithinSource(targetPath)) {
        report(module, `relative CSS @import must stay within src (${specifier})`);
        continue;
      }

      const target = describeModule(targetPath);
      validateLayerDependency(module, target, specifier, { cssImport: true });
    }
  }
}

const appFiles = modules.filter((module) => module.sourcePath.endsWith('/App.jsx') || module.sourcePath === 'App.jsx');
if (appFiles.length !== 1 || appFiles[0]?.sourcePath !== 'app/App.jsx') {
  violations.push(`src: expected exactly one app/App.jsx, found ${appFiles.map((module) => module.sourcePath).join(', ') || 'none'}`);
}

if (violations.length) {
  console.error('Import boundary violations:');
  violations.forEach((violation) => console.error(`  - ${violation}`));
  process.exitCode = 1;
} else {
  const codeModuleCount = modules.filter((module) => CODE_EXTENSIONS.has(extname(module.absolutePath))).length;
  const stylesheetCount = modules.length - codeModuleCount;
  console.log(`Import boundaries: ${codeModuleCount} code modules and ${stylesheetCount} stylesheets checked`);
}
