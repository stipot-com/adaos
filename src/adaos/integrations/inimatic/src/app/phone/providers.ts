export const providers = [
	{
		name: 'Elibrary',
		url: 'https://elibrary.ru/',
		checkLoginJs: `Boolean(document.querySelector("[href='author_info.asp?isold=1']"))`,
	},
]
