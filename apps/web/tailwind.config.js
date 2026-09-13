/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ['./components/PnlJournal.tsx'],
  important: '#pnl-journal',
  corePlugins: { preflight: false },
  theme: { extend: { colors: {
    vanilla: '#f0ead2', paper: '#fffdf7', cream: '#dde5b6', olive: '#546526',
    ink: '#27180f', muted: '#5f5147', copper: '#9a3412', moss: '#365314'
  } } },
};
