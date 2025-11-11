// src\adaos\integrations\inimatic\src\app\skill-host\ui-lib.ts
export function renderInto(root: HTMLElement, tree: any) {
	root.innerHTML = '';
	mountNode(root, tree);
}
function mountNode(parent: HTMLElement, node: any) {
	const t = node.type || 'div';
	const el = document.createElement(tagFor(t));
	if (node.id) el.id = node.id;
	applyProps(el, node);
	parent.appendChild(el);
	(node.children || []).forEach((c: any) => mountNode(el, c));
}
function tagFor(t: string) {
	const map: any = { stack: 'div', box: 'div', text: 'div', button: 'button', input: 'input', console: 'pre', canvas: 'canvas', table: 'table' };
	return map[t] || t;
}
function applyProps(el: HTMLElement, p: any) {
	const style: any = {};
	if (p.type === 'stack') { style.display = 'flex'; style.flexDirection = 'column'; style.gap = (p.gap ?? 8) + 'px'; style.padding = (p.padding ?? 0) + 'px'; }
	if (p.type === 'box') { style.border = p.border ?? '1px solid #ddd'; style.padding = (p.padding ?? 8) + 'px'; style.borderRadius = '12px'; }
	if (p.type === 'text' && p.text != null) el.textContent = String(p.text);
	if (p.type === 'button' && p.label != null) (el as HTMLButtonElement).textContent = String(p.label);
	if (p.type === 'input') (el as HTMLInputElement).placeholder = p.placeholder ?? '';
	if (p.type === 'console') { (el as HTMLPreElement).style.height = (p.height ?? 200) + 'px'; (el as HTMLPreElement).style.overflow = 'auto'; }
	Object.assign((el as any).style, style);
}
export function patch(root: HTMLElement, id: string, props: any) {
	const el = root.querySelector<HTMLElement>(`#${CSS.escape(id)}`); if (!el) return;
	if (props.text != null) el.textContent = String(props.text);
}
