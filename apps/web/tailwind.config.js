/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ['./components/PnlJournal.tsx'],
  important: '#pnl-journal',
  corePlugins: { preflight: false },
  theme: { extend: { colors: {
    vanilla: '#f0ead2', paper: '#fffdf7', cream: '#dde5b6', olive: '#546526',
    ink: '#27180f', muted: '#79695b', copper: '#a98467', moss: '#556b2f'
  } } },
};
