import React from 'react'
import ReactDOM from 'react-dom/client'
import { AuthProvider } from './auth'
import App from './App'
import './styles/index.css'

ReactDOM.createRoot(document.getElementById('root')).render(
    <AuthProvider>
        <App />
    </AuthProvider>
)
