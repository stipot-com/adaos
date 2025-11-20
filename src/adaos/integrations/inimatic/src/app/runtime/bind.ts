export function bindTemplate(tpl: string, ctx: any): string {
  if (!tpl || typeof tpl !== 'string') return tpl as any
  return tpl.replace(/@\{([^}]+)\}/g, (_, expr) => {
    const parts = String(expr).split('.')
    let v: any = ctx
    for (const p of parts) v = v?.[p]
    return (v ?? '').toString()
  })
}

