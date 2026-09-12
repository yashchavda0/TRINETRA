/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,jsx}'],
  darkMode: 'class',
  theme: {
    extend: {
      colors: {
        // Control-room palette: near-black surfaces so a bright video tile or a
        // red alert is the only thing that draws the eye.
        ink: {
          950: '#070a0e',
          900: '#0b0f14',
          850: '#0f1620',
          800: '#131c27',
          700: '#1f2a37',
          600: '#2b3847',
        },
        accent: {
          DEFAULT: '#2f81f7',
          soft: '#1d4ed8',
        },
        // Department colours, kept identical to the map marker palette in
        // GISMap.jsx so a legend chip and its marker cannot drift apart.
        dept: {
          police: '#e02020',
          rto: '#1d6fe0',
          gsrtc: '#f0a020',
          civil: '#17a94b',
          revenue: '#8a3ffc',
          private: '#6b7280',
        },
        state: {
          up: '#17a94b',
          down: '#e02020',
          warn: '#f0a020',
          unknown: '#6b7280',
        },
      },
      fontFamily: {
        sans: ['system-ui', '-apple-system', 'Segoe UI', 'sans-serif'],
        mono: ['ui-monospace', 'SFMono-Regular', 'Consolas', 'monospace'],
      },
      keyframes: {
        'pulse-alert': {
          '0%, 100%': { opacity: '1' },
          '50%': { opacity: '0.45' },
        },
      },
      animation: {
        'pulse-alert': 'pulse-alert 1.4s ease-in-out infinite',
      },
    },
  },
  plugins: [],
};
