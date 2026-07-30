import type { PackageStatus } from '../types.js';

export type PackageChangeLevel = 'patch' | 'minor' | 'major';
export type PackageChangeIntent = 'compatible_fix' | 'compatible_feature' | 'breaking_change';

interface ParsedVersion {
  major: bigint;
  minor: bigint;
  patch: bigint;
  prerelease?: string;
}

const VERSION_PATTERN = /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$/;
const RC_PATTERN = /^rc\.(0|[1-9]\d*)$/;

function parseVersion(value: string): ParsedVersion {
  const match = VERSION_PATTERN.exec(String(value ?? '').trim());
  if (!match) throw new Error('知识包版本不正确');
  return {
    major: BigInt(match[1]),
    minor: BigInt(match[2]),
    patch: BigInt(match[3]),
    ...(match[4] ? { prerelease: match[4] } : {}),
  };
}

function stableVersion(version: ParsedVersion): string {
  return `${version.major}.${version.minor}.${version.patch}`;
}

function bump(version: ParsedVersion, level: PackageChangeLevel): ParsedVersion {
  if (version.prerelease) throw new Error('请先完成或放弃当前发布候选');
  if (level === 'major') return { major: version.major + 1n, minor: 0n, patch: 0n };
  if (level === 'minor') return { major: version.major, minor: version.minor + 1n, patch: 0n };
  return { major: version.major, minor: version.minor, patch: version.patch + 1n };
}

export function recommendNextStableVersion(
  currentVersion: string,
  intent: PackageChangeIntent,
): string {
  const level: PackageChangeLevel = intent === 'breaking_change'
    ? 'major'
    : intent === 'compatible_feature' ? 'minor' : 'patch';
  return stableVersion(bump(parseVersion(currentVersion), level));
}

export function nextValidationCandidateVersion(input: {
  currentVersion: string;
  currentStatus: PackageStatus;
  changeLevel?: PackageChangeLevel;
}): string {
  const current = parseVersion(input.currentVersion);
  if (input.currentStatus === 'draft') return '1.0.0-rc.1';
  if (input.currentStatus === 'review') {
    const rc = current.prerelease ? RC_PATTERN.exec(current.prerelease) : null;
    if (rc) return `${stableVersion(current)}-rc.${BigInt(rc[1]) + 1n}`;
    if (!current.prerelease) return `${stableVersion(bump(current, 'patch'))}-rc.1`;
    throw new Error('评审版本必须使用 rc 发布候选');
  }
  if (input.currentStatus === 'released') {
    const target = bump(current, input.changeLevel ?? 'patch');
    return `${stableVersion(target)}-rc.1`;
  }
  throw new Error('已废弃版本不能继续创建发布候选');
}

export function promoteReleaseCandidate(value: string): string {
  const version = parseVersion(value);
  if (!version.prerelease || !RC_PATTERN.test(version.prerelease)) {
    throw new Error('只能正式发布 rc 发布候选');
  }
  return stableVersion(version);
}

export function isReleaseCandidateVersion(value: string): boolean {
  try {
    const version = parseVersion(value);
    return Boolean(version.prerelease && RC_PATTERN.test(version.prerelease));
  } catch {
    return false;
  }
}
