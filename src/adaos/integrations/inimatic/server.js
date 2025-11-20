// Import necessary modules
const app = require('./backend/app') // Make sure to adjust the path as needed
const debug = require('debug')('node-angular')
const http = require('http')

// Normalize a port into a number, string, or false.
const normalizePort = (val) => {
	const port = parseInt(val, 10)
	if (isNaN(port)) {
		// Named pipe
		return val
	}
	if (port >= 0) {
		// Port number
		return port
	}
	return false
}

// Event listener for HTTP server "error" event.
const onError = (error) => {
	if (error.syscall !== 'listen') {
		throw error
	}
	const addr = server.address()
	const bind = typeof addr === 'string' ? 'pipe ' + addr : 'port ' + port
	switch (error.code) {
		case 'EACCES':
			console.error(bind + ' requires elevated privileges')
			process.exit(1)
			break
		case 'EADDRINUSE':
			console.error(bind + ' is already in use')
			process.exit(1)
			break
		default:
			throw error
	}
}

// Event listener for HTTP server "listening" event.
const onListening = () => {
	const addr = server.address()
	const bind = typeof addr === 'string' ? 'pipe ' + addr : 'port ' + port
	debug('Listening on ' + bind)
}

// Get port from environment and store in Express.
const port = normalizePort(process.env.PORT || '3000')
app.set('port', port)

// Create HTTP server.
const server = http.createServer(app)

// Listen on provided port, on all network interfaces.
server.on('error', onError)
server.on('listening', onListening)
server.listen(port, '0.0.0.0', () =>
	console.log(`Started on http://localhost:${port} ...`)
)
