/* theme.js —— M1b 母版色板（浅色默认，深色同角色）+ 主题初始化（UI 批A）
   canvas 绘制读这里（与 app.css 的 CSS 变量同源）；主题切换 = html[data-theme] */

export const TRACK_COLORS_LIGHT = ['#D93A2B', '#3B3B38', '#8A8A84', '#B9B9B2', '#141414', '#CFCFC8', '#A55B4E', '#6E7B8A'];
export const TRACK_COLORS_DARK = ['#FF5040', '#E9EBEC', '#9AA2A8', '#6B737A', '#E9EBEC', '#4A4F55', '#E8A090', '#93A4B8'];

const P = {
  light: {
    rollBg: '#FAFAF8', rowWhite: '#FFFFFF', rowBlack: '#F1F1EE',
    barLine: '#C9C9C5', beatLine: '#ECECEA',
    keyWhite: '#F1F1EE', keyBlack: '#3B3B38', keySep: '#E3E3DF', keyLabel: '#9B9B96',
    rulerBg: '#F3F3F0', rulerText: '#5F5F5C', rulerTick: '#C9C9C5', rulerBar: '#3B3B38',
    playhead: '#D93A2B', selStroke: '#141414',
    noteStroke: 'rgba(20,20,20,0.45)',
    diffDelFill: 'rgba(179,39,30,0.16)', diffDelStroke: 'rgba(179,39,30,0.75)',
    diffAddFill: '#2E7D4F', diffAddStroke: '#2E7D4F',
    diffModStroke: '#8A6D1A',
    ghostFillMod: 'rgba(20,20,20,0.10)', ghostStrokeMod: '#141414',
    ghostFillMove: 'rgba(217,58,43,0.16)', ghostStrokeMove: '#D93A2B',
    ghostFillCreate: 'rgba(46,125,79,0.22)', ghostStrokeCreate: '#2E7D4F',
    residual: 'rgba(20,20,20,0.35)',
    segPhrase: 'rgba(217,58,43,0.22)', segPause: 'rgba(20,20,20,0.10)', segBreath: 'rgba(20,20,20,0.05)',
    selSoft: '#E9E9E5', panelBg: '#FAFAF8', laneLabel: '#9B9B96',
  },
  dark: {
    rollBg: '#1D1F21', rowWhite: '#212325', rowBlack: '#17181A',
    barLine: '#383C40', beatLine: '#282B2E',
    keyWhite: '#E9EBEC', keyBlack: '#101112', keySep: '#33373B', keyLabel: '#6B737A',
    rulerBg: '#232527', rulerText: '#9AA2A8', rulerTick: '#383C40', rulerBar: '#6B737A',
    playhead: '#FF5040', selStroke: '#E9EBEC',
    noteStroke: 'rgba(0,0,0,0.5)',
    diffDelFill: 'rgba(255,80,64,0.22)', diffDelStroke: 'rgba(255,80,64,0.85)',
    diffAddFill: '#3FBF8F', diffAddStroke: '#3FBF8F',
    diffModStroke: '#F5C518',
    ghostFillMod: 'rgba(233,235,236,0.12)', ghostStrokeMod: '#E9EBEC',
    ghostFillMove: 'rgba(255,80,64,0.18)', ghostStrokeMove: '#FF5040',
    ghostFillCreate: 'rgba(63,191,143,0.25)', ghostStrokeCreate: '#3FBF8F',
    residual: 'rgba(233,235,236,0.30)',
    segPhrase: 'rgba(255,80,64,0.30)', segPause: 'rgba(233,235,236,0.12)', segBreath: 'rgba(233,235,236,0.06)',
    selSoft: '#26292C', panelBg: '#212325', laneLabel: '#6B737A',
  },
};

export function themeName() { return document.documentElement.dataset.theme || 'light'; }
export function pal() { return themeName() === 'dark' ? P.dark : P.light; }
export function trackColors() { return themeName() === 'dark' ? TRACK_COLORS_DARK : TRACK_COLORS_LIGHT; }

/* 主题来源：?theme= > localStorage('tsov.theme') > 浅色默认（Q-UI-8 定案） */
export function initTheme() {
  const q = new URLSearchParams(location.search).get('theme');
  const saved = localStorage.getItem('tsov.theme');
  const t = (q === 'dark' || q === 'light') ? q : (saved === 'dark' ? 'dark' : 'light');
  document.documentElement.dataset.theme = t;
}

export function setTheme(t) {
  document.documentElement.dataset.theme = (t === 'dark' ? 'dark' : 'light');
  localStorage.setItem('tsov.theme', document.documentElement.dataset.theme);
  window.dispatchEvent(new Event('tsov-theme'));
}
