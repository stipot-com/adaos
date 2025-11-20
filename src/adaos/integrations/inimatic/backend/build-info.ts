import { execSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const BASE_VERSION = process.env['BACKEND_BASE_VERSION'] ?? '0.1.0';

const repoRoot = (() => {
        const currentDir = path.dirname(fileURLToPath(import.meta.url));
        return path.resolve(currentDir, '../../../../..');
})();

function git(...args: string[]): string | null {
        try {
                return execSync(['git', ...args].join(' '), {
                        cwd: repoRoot,
                        encoding: 'utf8',
                        stdio: ['ignore', 'pipe', 'ignore'],
                }).trim();
        } catch {
                return null;
        }
}

function computeVersion(): string {
        const explicit = process.env['BACKEND_BUILD_VERSION'];
        if (explicit && explicit.trim() !== '') {
                return explicit;
        }

        const revCount = git('rev-list --count HEAD');
        const shortSha = git('rev-parse --short HEAD');
        if (revCount) {
                const suffix = shortSha ? `+${revCount}.${shortSha}` : `+${revCount}`;
                return `${BASE_VERSION}${suffix}`;
        }

        return process.env['npm_package_version'] ?? BASE_VERSION;
}

function computeBuildDate(): string {
        const explicit = process.env['BACKEND_BUILD_DATE'];
        if (explicit && explicit.trim() !== '') {
                return explicit;
        }

        const commitDate = git('show -s --format=%cI HEAD');
        if (commitDate) {
                return commitDate;
        }

        return new Date().toISOString();
}

export const buildInfo = Object.freeze({
        version: computeVersion(),
        buildDate: computeBuildDate(),
        commit: git('rev-parse --short HEAD'),
});

