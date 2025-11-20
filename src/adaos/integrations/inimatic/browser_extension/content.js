window.addEventListener('message', (event) => {
	if (event.data?.type === 'set_session') {
		chrome.runtime.sendMessage(event.data)
		console.log(event, 'type' in event.data, event.data.type)
	}
})
