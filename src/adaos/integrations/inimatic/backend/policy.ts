export const POLICY = {
        max_archive_mb: 50,
        max_pr_per_day: 20,
        sandbox: {
                timeout_ms: 3000,
        },
        deps_allow: {
                pip: ['requests', 'pydantic'],
                npm: ['axios'],
        },
} as const

export type Policy = typeof POLICY

export function getPolicy(): Policy {
        return POLICY
}
