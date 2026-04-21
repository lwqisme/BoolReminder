"""
HTML报告生成器
生成美观的HTML表格报告，支持移动端显示
"""

from typing import Optional
from datetime import datetime
import sys
from pathlib import Path
import pytz

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent))
from drawdown.snapshot import DrawdownSnapshot
from watchlist_boll_filter import WatchlistBollFilterResult, StockInfo


def generate_html_report(
    result: WatchlistBollFilterResult,
    title: str = "BOLL指标筛选报告",
    drawdown_snapshots: Optional[dict[str, DrawdownSnapshot]] = None,
) -> str:
    """
    生成HTML格式的报告
    
    Args:
        result: BOLL筛选结果对象
        title: 报告标题
    
    Returns:
        HTML字符串
    """
    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title}</title>
    <style>
        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }}
        
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
            background: #f5f5f5;
            padding: 20px;
            line-height: 1.6;
            color: #333;
        }}
        
        .container {{
            max-width: 1200px;
            margin: 0 auto;
            background: white;
            border-radius: 8px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.1);
            overflow: hidden;
        }}
        
        .header {{
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 30px;
            text-align: center;
        }}
        
        .header h1 {{
            font-size: 28px;
            margin-bottom: 10px;
        }}
        
        .header .meta {{
            font-size: 14px;
            opacity: 0.9;
        }}
        
        .summary {{
            padding: 30px;
            background: #fafafa;
            border-bottom: 1px solid #e0e0e0;
        }}
        
        .summary-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 20px;
            margin-top: 20px;
        }}
        
        .summary-card {{
            background: white;
            padding: 20px;
            border-radius: 6px;
            border-left: 4px solid;
            box-shadow: 0 1px 3px rgba(0,0,0,0.1);
        }}
        
        .summary-card.below {{
            border-color: #e74c3c;
        }}
        
        .summary-card.near-lower {{
            border-color: #f39c12;
        }}
        
        .summary-card.near-upper {{
            border-color: #e67e22;
        }}
        
        .summary-card.above {{
            border-color: #3498db;
        }}
        
        .summary-card h3 {{
            font-size: 14px;
            color: #666;
            margin-bottom: 10px;
        }}
        
        .summary-card .count {{
            font-size: 32px;
            font-weight: bold;
            color: #333;
        }}

        .tab-nav {{
            display: flex;
            gap: 12px;
            padding: 14px 30px;
            border-bottom: 1px solid #e0e0e0;
            background: #fff;
            overflow-x: auto;
            -webkit-overflow-scrolling: touch;
        }}

        .tab-btn {{
            border: 1px solid #d8dee4;
            background: #f8f9fa;
            color: #495057;
            border-radius: 999px;
            padding: 8px 14px;
            font-size: 13px;
            font-weight: 600;
            cursor: pointer;
            white-space: nowrap;
            text-decoration: none;
            display: inline-block;
        }}

        .tab-btn .count {{
            margin-left: 6px;
            opacity: 0.8;
            font-weight: 700;
        }}

        .tab-btn.active {{
            color: #fff;
            border-color: transparent;
        }}

        .tab-btn.below.active {{
            background: #e74c3c;
        }}

        .tab-btn.above.active {{
            background: #3498db;
        }}

        .tab-btn.near-lower.active {{
            background: #f39c12;
        }}

        .tab-btn.near-upper.active {{
            background: #e67e22;
        }}

        body.tabs-enabled .section {{
            display: none;
        }}

        body.tabs-enabled .section.active {{
            display: block;
        }}
        
        .section {{
            padding: 30px;
            border-bottom: 1px solid #e0e0e0;
        }}
        
        .section:last-child {{
            border-bottom: none;
        }}
        
        .section-title {{
            font-size: 20px;
            margin-bottom: 20px;
            display: flex;
            align-items: center;
            gap: 10px;
        }}
        
        .section-title .icon {{
            font-size: 24px;
        }}
        
        .section.below .section-title {{
            color: #e74c3c;
        }}
        
        .section.near-lower .section-title {{
            color: #f39c12;
        }}
        
        .section.near-upper .section-title {{
            color: #e67e22;
        }}
        
        .section.above .section-title {{
            color: #3498db;
        }}
        
        table {{
            width: 100%;
            border-collapse: collapse;
            margin-top: 20px;
            background: white;
        }}
        
        th {{
            background: #f8f9fa;
            padding: 12px;
            text-align: left;
            font-weight: 600;
            color: #495057;
            border-bottom: 2px solid #dee2e6;
            font-size: 14px;
        }}
        
        td {{
            padding: 12px;
            border-bottom: 1px solid #e9ecef;
            font-size: 14px;
        }}
        
        tr:hover {{
            background: #f8f9fa;
        }}
        
        .badge {{
            display: inline-block;
            padding: 4px 8px;
            border-radius: 4px;
            font-size: 12px;
            font-weight: 600;
        }}
        
        .badge.warning {{
            background: #fff3cd;
            color: #856404;
        }}
        
        .empty {{
            text-align: center;
            padding: 40px;
            color: #999;
        }}
        
        .footer {{
            padding: 20px 30px;
            text-align: center;
            color: #999;
            font-size: 12px;
            background: #fafafa;
        }}
        
        @media (max-width: 768px) {{
            body {{
                padding: 6px;
            }}
            
            .container {{
                border-radius: 0;
                box-shadow: none;
            }}

            .header {{
                padding: 16px 12px;
            }}

            .header h1 {{
                font-size: 20px;
                margin-bottom: 6px;
            }}

            .header .meta {{
                font-size: 12px;
                line-height: 1.5;
            }}

            .summary {{
                padding: 14px 12px;
            }}

            .summary h2 {{
                font-size: 18px;
            }}

            .summary-grid {{
                gap: 10px;
                margin-top: 12px;
            }}

            .summary-card {{
                padding: 12px;
            }}

            .summary-card h3 {{
                font-size: 13px;
                margin-bottom: 6px;
            }}

            .summary-card .count {{
                font-size: 26px;
            }}

            .tab-nav {{
                padding: 10px 10px;
                gap: 8px;
            }}

            .tab-btn {{
                padding: 7px 11px;
                font-size: 12px;
            }}

            .section {{
                padding: 14px 10px;
            }}

            .section-title {{
                font-size: 17px;
                margin-bottom: 10px;
                gap: 8px;
            }}

            .section-title .icon {{
                font-size: 20px;
            }}
            
            .summary-grid {{
                grid-template-columns: 1fr;
            }}
            
            table {{
                display: block;
                overflow-x: auto;
                -webkit-overflow-scrolling: touch;
                white-space: nowrap;
                font-size: 12px;
                margin-top: 12px;
            }}
            
            th, td {{
                padding: 7px 8px;
            }}
            
            .footer {{
                padding: 12px 10px;
                font-size: 11px;
            }}
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>{title}</h1>
            <div class="meta">
                更新时间: {result.update_time} | 
                分析总数: {result.total_analyzed} | 
                筛选结果: {result.total_found} 只
            </div>
        </div>
        
        <div class="summary">
            <h2>📊 统计汇总</h2>
            <div class="summary-grid">
                <div class="summary-card below">
                    <h3>🔴 低于下轨（超卖）</h3>
                    <div class="count">{len(result.below_lower)}</div>
                </div>
                <div class="summary-card above">
                    <h3>🔵 超出上轨（超买）</h3>
                    <div class="count">{len(result.above_upper)}</div>
                </div>
                <div class="summary-card near-lower">
                    <h3>🟡 接近下轨</h3>
                    <div class="count">{len(result.near_lower)}</div>
                </div>
                <div class="summary-card near-upper">
                    <h3>🟠 接近上轨</h3>
                    <div class="count">{len(result.near_upper)}</div>
                </div>
            </div>
            <div style="margin-top: 20px; color: #666; font-size: 14px;">
                配置参数: 周期={result.period}, k={result.k}, 阈值={result.threshold * 100}%
            </div>
        </div>

        <div class="tab-nav" role="tablist" aria-label="分类切换">
            <a class="tab-btn below" data-tab="below" href="#section-below">🔴 低于下轨<span class="count">{len(result.below_lower)}</span></a>
            <a class="tab-btn above" data-tab="above" href="#section-above">🔵 超出上轨<span class="count">{len(result.above_upper)}</span></a>
            <a class="tab-btn near-lower" data-tab="near-lower" href="#section-near-lower">🟡 接近下轨<span class="count">{len(result.near_lower)}</span></a>
            <a class="tab-btn near-upper" data-tab="near-upper" href="#section-near-upper">🟠 接近上轨<span class="count">{len(result.near_upper)}</span></a>
        </div>
        
        {_generate_section_html("below", "低于下轨 - 超卖区域", result.below_lower, "distance_from_lower_pct", True, drawdown_snapshots)}
        {_generate_section_html("above", "超出上轨 - 超买区域", result.above_upper, "distance_from_upper_pct", True, drawdown_snapshots)}
        {_generate_section_html("near-lower", "接近下轨", result.near_lower, "distance_from_lower_pct", False, drawdown_snapshots)}
        {_generate_section_html("near-upper", "接近上轨", result.near_upper, "distance_from_upper_pct", False, drawdown_snapshots)}
        
        <div class="footer">
            <p>本报告由BOLL指标筛选系统自动生成</p>
            <p>生成时间: {datetime.now(pytz.timezone('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M:%S')}</p>
        </div>
    </div>
    <script>
        (function() {{
            const tabButtons = document.querySelectorAll('.tab-btn');
            const sections = document.querySelectorAll('.section');
            if (!tabButtons.length || !sections.length) {{
                return;
            }}

            document.body.classList.add('tabs-enabled');

            function activateTab(tabName) {{
                tabButtons.forEach((button) => {{
                    button.classList.toggle('active', button.dataset.tab === tabName);
                }});
                sections.forEach((section) => {{
                    section.classList.toggle('active', section.dataset.tab === tabName);
                }});
            }}

            tabButtons.forEach((button) => {{
                button.addEventListener('click', (event) => {{
                    event.preventDefault();
                    const tabName = button.dataset.tab;
                    activateTab(tabName);
                    const targetId = button.getAttribute('href');
                    if (targetId && window.history && window.history.replaceState) {{
                        window.history.replaceState(null, '', targetId);
                    }}
                }});
            }});

            const hashTabMap = {{
                '#section-below': 'below',
                '#section-above': 'above',
                '#section-near-lower': 'near-lower',
                '#section-near-upper': 'near-upper'
            }};
            const tabFromHash = hashTabMap[window.location.hash] || '';
            const firstTabWithData = Array.from(tabButtons).find((button) => {{
                const countNode = button.querySelector('.count');
                const count = countNode ? Number(countNode.textContent) : 0;
                return count > 0;
            }});
            activateTab(tabFromHash || (firstTabWithData || tabButtons[0]).dataset.tab);
        }})();
    </script>
</body>
</html>"""
    
    return html


def _format_drawdown_percent(value: float | None) -> str:
    if value is None:
        return "--"
    return f"{value:.2f}%"


def _generate_section_html(
    section_class: str,
    title: str,
    stocks: list,
    distance_field: str,
    is_extreme: bool,
    drawdown_snapshots: Optional[dict[str, DrawdownSnapshot]] = None,
) -> str:
    """生成单个分类的HTML"""
    icon_map = {
        "below": "🔴",
        "near-lower": "🟡",
        "near-upper": "🟠",
        "above": "🔵"
    }
    
    icon = icon_map.get(section_class, "📊")
    
    html = f"""
        <div class="section {section_class}" data-tab="{section_class}" id="section-{section_class}">
            <div class="section-title">
                <span class="icon">{icon}</span>
                <span>{title} ({len(stocks)} 只)</span>
            </div>
    """

    if not stocks:
        html += """
            <div class="empty">暂无符合条件的股票</div>
        </div>
    """
        return html

    html += """
            <table>
                <thead>
                    <tr>
                        <th>股票名称</th>
                        <th>当前价格</th>
                        <th>距离</th>
                        <th>ATH回撤</th>
                        <th>120d回撤</th>
                        <th>下轨</th>
                        <th>中轨</th>
                        <th>上轨</th>
                    </tr>
                </thead>
                <tbody>
    """
    
    for stock in stocks:
        display_name = stock.display_name[:30] if len(stock.display_name) > 30 else stock.display_name
        snapshot = (drawdown_snapshots or {}).get(stock.symbol)
        
        if distance_field == "distance_from_lower_pct":
            distance = stock.distance_from_lower_pct
            distance_str = f"{distance:.2f}%"
        else:
            distance = stock.distance_from_upper_pct
            distance_str = f"{abs(distance):.2f}%"
        
        warning_badge = '<span class="badge warning">⚠️</span>' if is_extreme else ''
        
        html += f"""
                    <tr>
                        <td><strong>{display_name}</strong></td>
                        <td>{stock.currency_symbol}{stock.current_price:.2f}</td>
                        <td>{distance_str} {warning_badge}</td>
                        <td>{_format_drawdown_percent(snapshot.drawdown_ath_pct if snapshot else None)}</td>
                        <td>{_format_drawdown_percent(snapshot.drawdown_120_pct if snapshot else None)}</td>
                        <td>{stock.currency_symbol}{stock.lower_band:.4f}</td>
                        <td>{stock.currency_symbol}{stock.mid_band:.4f}</td>
                        <td>{stock.currency_symbol}{stock.upper_band:.4f}</td>
                    </tr>
        """
    
    html += """
                </tbody>
            </table>
        </div>
    """
    
    return html


def save_html_report(
    result: WatchlistBollFilterResult,
    output_path: str,
    title: str = "BOLL指标筛选报告",
    drawdown_snapshots: Optional[dict[str, DrawdownSnapshot]] = None,
) -> None:
    """
    生成并保存HTML报告到文件
    
    Args:
        result: BOLL筛选结果对象
        output_path: 输出文件路径
        title: 报告标题
    """
    html = generate_html_report(result, title, drawdown_snapshots=drawdown_snapshots)
    
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(html)
