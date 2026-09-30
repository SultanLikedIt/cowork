import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App.jsx'
import './index.css'

// Dark-first: the product ships dark, and tailwind.config uses the class
// strategy (not media) so the OS preference cannot silently override it.
// The light token set in index.css stays complete, so flipping this class
// is all a future theme toggle needs.
document.documentElement.classList.add('dark')
document.documentElement.style.colorScheme = 'dark'

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
)
