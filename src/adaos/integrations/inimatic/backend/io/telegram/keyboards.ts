export type InlineKeyboardButton = { text: string, callback_data?: string }

export function keyboardPicker(items: { alias: string, is_default?: boolean }[]): { inline_keyboard: InlineKeyboardButton[][] } {
  const rows: InlineKeyboardButton[][] = []
  for (const it of items) {
    const label = `${it.is_default ? '⭐ ' : ''}${it.alias}`
    rows.push([
      { text: `Текущая: ${label}`, callback_data: `use:${it.alias}` },
      { text: `Дефолт: ${label}`, callback_data: `def:${it.alias}` },
    ])
  }
  return { inline_keyboard: rows }
}

