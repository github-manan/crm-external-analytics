// Shared Chart.js theme for every page. Colors come from CSS custom
// properties in style.css (--series-*, --ramp-*, --chart-*), so light and
// dark each get their own validated steps and the pages never hardcode hex.
//
// Palette rules (validated with the dataviz palette checker, both themes):
//   - a single-series chart is always --series-1 (blue)
//   - --series-2 (orange) is the one contrast color: a second series, or a
//     highlighted subset of a single series
//   - ordered steps (the lead funnel) use the one-hue --ramp-1..5
// Loaded after theme.js and Chart.js, before the page's own script.

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

const CHART = {
  series: [cssVar('--series-1'), cssVar('--series-2'), cssVar('--series-3')],
  ramp: [1, 2, 3, 4, 5].map((i) => cssVar(`--ramp-${i}`)),
  grid: cssVar('--chart-grid'),
  axis: cssVar('--chart-axis'),
  tick: cssVar('--text-muted'),
  good: cssVar('--good'),
  bad: cssVar('--bad'),
  // Translucent area fill under a line, from a #rrggbb series color.
  fill: (hex) => hex + '24',
};

Chart.defaults.font.family = "Inter, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif";
Chart.defaults.font.size = 11;
Chart.defaults.color = CHART.tick;
Chart.defaults.borderColor = CHART.grid;
// Every canvas sits in a fixed-height .chart-box; aspect-ratio sizing is
// what made the charts a full screen tall on a wide monitor.
Chart.defaults.maintainAspectRatio = false;
Chart.defaults.plugins.legend.labels.usePointStyle = true;
Chart.defaults.plugins.legend.labels.boxWidth = 8;
Chart.defaults.plugins.legend.labels.boxHeight = 8;
Chart.defaults.plugins.tooltip.backgroundColor = cssVar('--tooltip-bg');
Chart.defaults.plugins.tooltip.titleColor = cssVar('--tooltip-text');
Chart.defaults.plugins.tooltip.bodyColor = cssVar('--tooltip-text');
Chart.defaults.plugins.tooltip.padding = 8;
Chart.defaults.plugins.tooltip.cornerRadius = 6;
Chart.defaults.plugins.tooltip.displayColors = false;
Chart.defaults.elements.line.borderWidth = 2;
Chart.defaults.elements.point.radius = 0;
Chart.defaults.elements.point.hoverRadius = 4;
Chart.defaults.elements.bar.borderRadius = 4;
Chart.defaults.elements.bar.borderSkipped = 'start';
Chart.defaults.datasets.bar.maxBarThickness = 28;
Chart.defaults.datasets.bar.categoryPercentage = 0.75;
// Line charts: hovering anywhere in a column shows that column's tooltip.
Chart.defaults.datasets.line.pointHitRadius = 8;
Chart.defaults.interaction.mode = 'index';
Chart.defaults.interaction.intersect = false;

function baseGrid(axis) {
  return {
    grid: { color: CHART.grid, drawTicks: false },
    border: { display: false },
    ticks: { color: CHART.tick, padding: 6 },
    ...axis,
  };
}

// Horizontal bar charts get taller with their number of bars, so a
// 3-stage pipeline isn't stretched to the height of a 12-status list.
function fitBarHeight(canvas, bars, perBar = 26, extra = 44) {
  canvas.parentElement.style.height = `${Math.max(120, bars * perBar + extra)}px`;
}
