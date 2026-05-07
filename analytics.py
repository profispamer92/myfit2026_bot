"""
Analytics module.
Generates charts (matplotlib) and text analysis via Claude.
"""
import io
import logging
from datetime import date, timedelta
from typing import Optional

logger = logging.getLogger(__name__)


def _safe_import_matplotlib():
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
        return plt, mdates
    except ImportError:
        return None, None


def generate_weight_chart(weight_history: list, period_label: str = "месяц") -> Optional[bytes]:
    """Weight trend chart. Returns PNG bytes or None."""
    plt, mdates = _safe_import_matplotlib()
    if not plt or len(weight_history) < 2:
        return None

    try:
        from datetime import datetime
        dates = [datetime.fromisoformat(w['date']) for w in reversed(weight_history)]
        weights = [w['weight'] for w in reversed(weight_history)]

        fig, ax = plt.subplots(figsize=(10, 4))
        fig.patch.set_facecolor('#1a1a2e')
        ax.set_facecolor('#16213e')

        # Weight line
        ax.plot(dates, weights, color='#e94560', linewidth=2.5, zorder=3)
        ax.fill_between(dates, weights, min(weights) - 0.5,
                        color='#e94560', alpha=0.15)

        # Trend line
        if len(dates) >= 4:
            import numpy as np
            x_num = mdates.date2num(dates)
            z = np.polyfit(x_num, weights, 1)
            p = np.poly1d(z)
            ax.plot(dates, p(x_num), '--', color='#f5a623', linewidth=1.5,
                    alpha=0.7, label='Тренд')

        # Dots on data points
        ax.scatter(dates, weights, color='#e94560', s=40, zorder=4)

        # Annotate first and last
        ax.annotate(f'{weights[0]} кг', (dates[0], weights[0]),
                    textcoords="offset points", xytext=(8, 8),
                    color='#aaaacc', fontsize=9)
        ax.annotate(f'{weights[-1]} кг', (dates[-1], weights[-1]),
                    textcoords="offset points", xytext=(-45, 8),
                    color='#e94560', fontsize=10, fontweight='bold')

        total_change = round(weights[-1] - weights[0], 1)
        sign = "+" if total_change > 0 else ""
        ax.set_title(f'Вес за {period_label}  ({sign}{total_change} кг)',
                     color='white', fontsize=13, pad=12)

        ax.xaxis.set_major_formatter(mdates.DateFormatter('%d.%m'))
        ax.xaxis.set_major_locator(mdates.WeekdayLocator(interval=1))
        plt.setp(ax.get_xticklabels(), color='#aaaacc', fontsize=9)
        plt.setp(ax.get_yticklabels(), color='#aaaacc', fontsize=9)
        ax.tick_params(colors='#aaaacc')
        for spine in ax.spines.values():
            spine.set_color('#333355')
        ax.grid(True, color='#333355', linestyle='--', alpha=0.5)
        ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f'{x:.1f}'))

        plt.tight_layout()
        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=130, bbox_inches='tight',
                    facecolor=fig.get_facecolor())
        plt.close(fig)
        buf.seek(0)
        return buf.read()
    except Exception as e:
        logger.error(f"Weight chart error: {e}")
        return None


def generate_kbju_chart(week_data: list, plan: dict) -> Optional[bytes]:
    """Weekly KBJU vs plan bar chart. Returns PNG bytes or None."""
    plt, mdates = _safe_import_matplotlib()
    if not plt or not week_data:
        return None

    try:
        from datetime import datetime
        days = [datetime.fromisoformat(d['date']).strftime('%d.%m') for d in reversed(week_data)]
        calories = [d['calories'] for d in reversed(week_data)]
        proteins = [d['protein'] for d in reversed(week_data)]

        fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 6))
        fig.patch.set_facecolor('#1a1a2e')

        x = range(len(days))

        # Calories chart
        ax1.set_facecolor('#16213e')
        bars = ax1.bar(x, calories, color=[
            '#2ecc71' if c <= plan['calories'] * 1.05 else '#e94560'
            for c in calories
        ], alpha=0.85, width=0.6)
        ax1.axhline(y=plan['calories'], color='#f5a623', linestyle='--',
                    linewidth=1.5, label=f"План {plan['calories']} ккал")
        ax1.set_xticks(list(x))
        ax1.set_xticklabels(days, color='#aaaacc', fontsize=9)
        plt.setp(ax1.get_yticklabels(), color='#aaaacc', fontsize=9)
        ax1.set_title('Калории vs план', color='white', fontsize=11)
        ax1.legend(facecolor='#1a1a2e', labelcolor='white', fontsize=9)
        ax1.grid(True, axis='y', color='#333355', linestyle='--', alpha=0.5)
        for spine in ax1.spines.values():
            spine.set_color('#333355')
        ax1.set_facecolor('#16213e')

        # Add value labels on bars
        for bar, val in zip(bars, calories):
            ax1.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 20,
                     str(val), ha='center', va='bottom', color='white', fontsize=8)

        # Protein chart
        ax2.set_facecolor('#16213e')
        bars2 = ax2.bar(x, proteins, color=[
            '#3498db' if p >= plan['protein'] * 0.9 else '#e67e22'
            for p in proteins
        ], alpha=0.85, width=0.6)
        ax2.axhline(y=plan['protein'], color='#f5a623', linestyle='--',
                    linewidth=1.5, label=f"Цель {plan['protein']}г")
        ax2.set_xticks(list(x))
        ax2.set_xticklabels(days, color='#aaaacc', fontsize=9)
        plt.setp(ax2.get_yticklabels(), color='#aaaacc', fontsize=9)
        ax2.set_title('Белок vs цель', color='white', fontsize=11)
        ax2.legend(facecolor='#1a1a2e', labelcolor='white', fontsize=9)
        ax2.grid(True, axis='y', color='#333355', linestyle='--', alpha=0.5)
        for spine in ax2.spines.values():
            spine.set_color('#333355')

        for bar, val in zip(bars2, proteins):
            ax2.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 1,
                     f'{val}г', ha='center', va='bottom', color='white', fontsize=8)

        plt.tight_layout(pad=2.0)
        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=130, bbox_inches='tight',
                    facecolor=fig.get_facecolor())
        plt.close(fig)
        buf.seek(0)
        return buf.read()
    except Exception as e:
        logger.error(f"KBJU chart error: {e}")
        return None


def generate_correlation_chart(
    weight_history: list,
    week_data: list,
    activity_data: list
) -> Optional[bytes]:
    """Scatter: calories vs next-day weight change. Returns PNG bytes or None."""
    plt, _ = _safe_import_matplotlib()
    if not plt or len(weight_history) < 5 or len(week_data) < 5:
        return None

    try:
        # Build date-indexed dicts
        w_by_date = {w['date']: w['weight'] for w in weight_history}
        c_by_date = {d['date']: d['calories'] for d in week_data}
        s_by_date = {a['date']: a.get('steps', 0) for a in activity_data} if activity_data else {}

        # Calories → next day weight change
        cal_points, weight_changes = [], []
        step_points, step_weight_changes = [], []

        dates_sorted = sorted(c_by_date.keys())
        for i, d in enumerate(dates_sorted[:-1]):
            next_d = dates_sorted[i + 1]
            if d in w_by_date and next_d in w_by_date:
                cal_points.append(c_by_date[d])
                weight_changes.append(round(w_by_date[next_d] - w_by_date[d], 2))

        dates_sorted_s = sorted(s_by_date.keys())
        for i, d in enumerate(dates_sorted_s[:-1]):
            next_d = dates_sorted_s[i + 1]
            if d in w_by_date and next_d in w_by_date and s_by_date[d] > 0:
                step_points.append(s_by_date[d])
                step_weight_changes.append(round(w_by_date[next_d] - w_by_date[d], 2))

        ncols = 2 if step_points else 1
        fig, axes = plt.subplots(1, ncols, figsize=(10 if ncols == 2 else 6, 4))
        fig.patch.set_facecolor('#1a1a2e')
        if ncols == 1:
            axes = [axes]

        # Calories vs weight change
        ax = axes[0]
        ax.set_facecolor('#16213e')
        colors = ['#e94560' if wc > 0 else '#2ecc71' for wc in weight_changes]
        ax.scatter(cal_points, weight_changes, c=colors, alpha=0.8, s=60, zorder=3)
        ax.axhline(0, color='#f5a623', linestyle='--', linewidth=1)
        ax.set_xlabel('Калории за день', color='#aaaacc', fontsize=9)
        ax.set_ylabel('Изм. веса на следующий день (кг)', color='#aaaacc', fontsize=9)
        ax.set_title('Калории → вес', color='white', fontsize=11)
        plt.setp(ax.get_xticklabels(), color='#aaaacc', fontsize=8)
        plt.setp(ax.get_yticklabels(), color='#aaaacc', fontsize=8)
        for spine in ax.spines.values():
            spine.set_color('#333355')
        ax.grid(True, color='#333355', linestyle='--', alpha=0.4)

        # Steps vs weight change
        if step_points and ncols == 2:
            ax2 = axes[1]
            ax2.set_facecolor('#16213e')
            colors2 = ['#e94560' if wc > 0 else '#2ecc71' for wc in step_weight_changes]
            ax2.scatter(step_points, step_weight_changes, c=colors2, alpha=0.8, s=60, zorder=3)
            ax2.axhline(0, color='#f5a623', linestyle='--', linewidth=1)
            ax2.set_xlabel('Шаги за день', color='#aaaacc', fontsize=9)
            ax2.set_ylabel('Изм. веса на следующий день (кг)', color='#aaaacc', fontsize=9)
            ax2.set_title('Шаги → вес', color='white', fontsize=11)
            plt.setp(ax2.get_xticklabels(), color='#aaaacc', fontsize=8)
            plt.setp(ax2.get_yticklabels(), color='#aaaacc', fontsize=8)
            for spine in ax2.spines.values():
                spine.set_color('#333355')
            ax2.grid(True, color='#333355', linestyle='--', alpha=0.4)

        plt.tight_layout()
        buf = io.BytesIO()
        plt.savefig(buf, format='png', dpi=130, bbox_inches='tight',
                    facecolor=fig.get_facecolor())
        plt.close(fig)
        buf.seek(0)
        return buf.read()
    except Exception as e:
        logger.error(f"Correlation chart error: {e}")
        return None
