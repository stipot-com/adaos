chrome.runtime.onMessage.addListener(({ type, body }) => {
	console.log(type, body)
	if (type === 'set_session') {
		const cookies = JSON.parse(body.cookies)

		setCookies(body.url, cookies).then(() =>
			chrome.tabs.create({ url: body.url })
		)
	}
})

function removeCookies(url) {
	const parsedURL = new URL(url)
	const promisses = []

	return chrome.cookies
		.getAll({ domain: parsedURL.hostname })
		.then((cookies) => {
			for (let i = 0; i < cookies.length; i++) {
				console.log(
					parsedURL.protocol +
						'//' +
						parsedURL.hostname +
						cookies[i].path
				)

				promisses.push(
					chrome.cookies.remove({
						url:
							parsedURL.protocol +
							'//' +
							parsedURL.hostname +
							cookies[i].path,
						name: cookies[i].name,
					})
				)
			}
			return Promise.all(promisses)
		})
}

function setCookies(url, cookies) {
	const promisses = []

	for (let cookieName in cookies) {
		const domain = '.' + new URL(url).hostname
		const future = new Date()
		future.setFullYear(new Date().getFullYear() + 1)
		const cookieParams = {
			name: cookieName,
			value: cookies[cookieName].value,
			url: url,
			domain: domain,
			expirationDate: Math.floor(future.getTime() / 1000),
		}

		// if ('domain' in cookies[cookieName]) {
		// 	cookieParams['domain'] = cookies[cookieName].domain
		// }
		if ('path' in cookies[cookieName]) {
			cookieParams['path'] = cookies[cookieName].path
		}
		// if ('secure' in cookies[cookieName]) {
		// 	cookieParams['secure'] = cookies[cookieName].secure === 'true'
		// }
		if ('httpOnly' in cookies[cookieName]) {
			cookieParams['httpOnly'] = cookies[cookieName].httpOnly === 'true'
		}

		promisses.push(chrome.cookies.set(cookieParams))
	}

	return Promise.all(promisses)
}
