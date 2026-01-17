"""
Flask Web应用
提供Web界面查看结果、更新token、手动触发分析
"""

import os
import sys
from pathlib import Path
from flask import Flask, render_template_string, jsonify, request, session, redirect, url_for
from typing import Optional
from datetime import datetime

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent))
from config.config_manager import ConfigManager
from watchlist_boll_filter import main, run_analysis_and_notify, WatchlistBollFilterResult
from report.html_generator import generate_html_report

# 全局变量
app = Flask(__name__)
config_manager: Optional[ConfigManager] = None
latest_result: Optional[WatchlistBollFilterResult] = None
scheduler_instance = None  # 全局调度器实例，用于动态更新

# 初始化配置管理器（模块级别）
try:
    config_manager = ConfigManager()
except Exception as e:
    print(f"警告: 配置管理器初始化失败: {e}")

# 启动时加载最新的分析结果
try:
    from watchlist_boll_filter import load_latest_result
    loaded_result = load_latest_result()
    if loaded_result:
        latest_result = loaded_result
        print(f"已加载最新分析结果: {latest_result.update_time}")
    else:
        print("未找到历史分析结果，将在首次分析后显示")
except Exception as e:
    print(f"加载最新结果失败: {e}")
    import traceback
    traceback.print_exc()


def init_app():
    """初始化Flask应用"""
    global config_manager
    
    if config_manager is None:
        config_manager = ConfigManager()
    
    web_config = config_manager.get_web_config()
    
    # 设置Flask secret_key
    secret_key = web_config.get("secret_key")
    if not secret_key:
        # 如果没有配置，生成一个随机密钥（仅用于开发）
        secret_key = os.urandom(24).hex()
        print("警告: 未配置secret_key，使用临时密钥。生产环境请设置固定密钥。")
    app.secret_key = secret_key


# HTML模板
INDEX_TEMPLATE = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>BOLL指标筛选系统</title>
    <style>
        body { font-family: Arial, sans-serif; margin: 20px; background: #f5f5f5; }
        .container { max-width: 1200px; margin: 0 auto; background: white; padding: 20px; border-radius: 8px; }
        .header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; }
        .btn { padding: 10px 20px; background: #667eea; color: white; border: none; border-radius: 4px; cursor: pointer; text-decoration: none; display: inline-block; }
        .btn:hover { background: #5568d3; }
        .btn-secondary { background: #6c757d; }
        .btn-secondary:hover { background: #5a6268; }
        .status { padding: 10px; margin: 10px 0; border-radius: 4px; }
        .status.success { background: #d4edda; color: #155724; }
        .status.error { background: #f8d7da; color: #721c24; }
        .status.info { background: #d1ecf1; color: #0c5460; }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>BOLL指标筛选系统</h1>
            <div>
                <a href="/history" class="btn btn-secondary">历史报告</a>
                <a href="/schedule" class="btn btn-secondary">定时任务</a>
                <a href="/update-token" class="btn btn-secondary">更新Token</a>
                <button onclick="triggerAnalysis()" class="btn">手动触发分析</button>
            </div>
        </div>
        
        <div id="status"></div>
        
        <div id="content">
            {% if result %}
                {{ result_html|safe }}
            {% else %}
                <p>暂无分析结果。点击"手动触发分析"按钮开始分析。</p>
            {% endif %}
        </div>
    </div>
    
    <script>
        function triggerAnalysis() {
            document.getElementById('status').innerHTML = '<div class="status info">正在分析，请稍候...</div>';
            fetch('/api/trigger', { method: 'POST' })
                .then(response => response.json())
                .then(data => {
                    if (data.success) {
                        location.reload();
                    } else {
                        document.getElementById('status').innerHTML = '<div class="status error">' + data.message + '</div>';
                    }
                })
                .catch(error => {
                    document.getElementById('status').innerHTML = '<div class="status error">请求失败: ' + error + '</div>';
                });
        }
    </script>
</body>
</html>
"""

HISTORY_TEMPLATE = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>历史报告 - BOLL指标筛选系统</title>
    <style>
        body { font-family: Arial, sans-serif; margin: 20px; background: #f5f5f5; }
        .container { max-width: 1200px; margin: 0 auto; background: white; padding: 20px; border-radius: 8px; }
        .header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; }
        .btn { padding: 10px 20px; background: #667eea; color: white; border: none; border-radius: 4px; cursor: pointer; text-decoration: none; display: inline-block; }
        .btn:hover { background: #5568d3; }
        .btn-secondary { background: #6c757d; }
        .btn-secondary:hover { background: #5a6268; }
        .report-list { margin-top: 20px; }
        .report-item { 
            padding: 15px; 
            margin-bottom: 10px; 
            border: 1px solid #ddd; 
            border-radius: 4px; 
            display: flex; 
            justify-content: space-between; 
            align-items: center;
            background: #fafafa;
            transition: background 0.2s;
        }
        .report-item:hover { background: #f0f0f0; }
        .report-info { flex: 1; }
        .report-name { font-weight: bold; font-size: 16px; color: #333; }
        .report-time { color: #666; font-size: 14px; margin-top: 5px; }
        .report-actions { display: flex; gap: 10px; }
        .empty { text-align: center; padding: 40px; color: #999; }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>历史报告</h1>
            <div>
                <a href="/" class="btn btn-secondary">返回首页</a>
            </div>
        </div>
        
        <div class="report-list">
            {% if reports %}
                {% for report in reports %}
                <div class="report-item">
                    <div class="report-info">
                        <div class="report-name">{{ report.name }}</div>
                        <div class="report-time">生成时间: {{ report.time }}</div>
                    </div>
                    <div class="report-actions">
                        <a href="/report/{{ report.filename }}" class="btn" target="_blank">查看</a>
                    </div>
                </div>
                {% endfor %}
            {% else %}
                <div class="empty">
                    <p>暂无历史报告</p>
                    <p style="margin-top: 10px;"><a href="/">返回首页</a> 手动触发分析生成报告</p>
                </div>
            {% endif %}
        </div>
    </div>
</body>
</html>
"""

SCHEDULE_TEMPLATE = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>定时任务配置 - BOLL指标筛选系统</title>
    <style>
        body { font-family: Arial, sans-serif; margin: 20px; background: #f5f5f5; }
        .container { max-width: 600px; margin: 0 auto; background: white; padding: 30px; border-radius: 8px; }
        .form-group { margin-bottom: 20px; }
        label { display: block; margin-bottom: 5px; font-weight: bold; }
        input[type="number"] { width: 100px; padding: 10px; border: 1px solid #ddd; border-radius: 4px; box-sizing: border-box; }
        .btn { padding: 10px 20px; background: #667eea; color: white; border: none; border-radius: 4px; cursor: pointer; }
        .btn:hover { background: #5568d3; }
        .status { padding: 10px; margin: 10px 0; border-radius: 4px; }
        .status.success { background: #d4edda; color: #155724; }
        .status.error { background: #f8d7da; color: #721c24; }
        .status.info { background: #d1ecf1; color: #0c5460; }
        .back-link { margin-top: 20px; }
        .info-box { background: #f8f9fa; padding: 15px; border-radius: 4px; margin-bottom: 20px; }
        .info-box p { margin: 5px 0; color: #666; }
        .time-input-group { display: flex; align-items: center; gap: 10px; }
        .time-input-group input { width: 80px; }
    </style>
</head>
<body>
    <div class="container">
        <h1>定时任务配置</h1>
        <div id="status"></div>
        
        <div class="info-box">
            <p><strong>当前配置：</strong></p>
            <p>执行时间: <span id="current-time">{{ current_hour }}:{{ current_minute }}</span> (北京时间)</p>
            <p>下次执行: <span id="next-run">{{ next_run_time }}</span></p>
        </div>
        
        <form id="scheduleForm" onsubmit="updateSchedule(event)">
            <div class="form-group">
                <label for="hour">执行小时（0-23）：</label>
                <input type="number" id="hour" name="hour" min="0" max="23" value="{{ current_hour }}" required>
            </div>
            <div class="form-group">
                <label for="minute">执行分钟（0-59）：</label>
                <input type="number" id="minute" name="minute" min="0" max="59" value="{{ current_minute }}" required>
            </div>
            <button type="submit" class="btn">保存配置</button>
        </form>
        
        <div class="back-link">
            <a href="/">返回首页</a>
        </div>
    </div>
    
    <script>
        function updateSchedule(event) {
            event.preventDefault();
            const hour = parseInt(document.getElementById('hour').value);
            const minute = parseInt(document.getElementById('minute').value);
            
            if (hour < 0 || hour > 23) {
                document.getElementById('status').innerHTML = '<div class="status error">小时必须在0-23之间</div>';
                return;
            }
            
            if (minute < 0 || minute > 59) {
                document.getElementById('status').innerHTML = '<div class="status error">分钟必须在0-59之间</div>';
                return;
            }
            
            fetch('/api/schedule', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ hour: hour, minute: minute })
            })
            .then(response => response.json())
            .then(data => {
                const statusDiv = document.getElementById('status');
                if (data.success) {
                    statusDiv.innerHTML = '<div class="status success">定时任务配置已更新！</div>';
                    // 更新显示
                    document.getElementById('current-time').textContent = hour + ':' + (minute < 10 ? '0' + minute : minute);
                    if (data.next_run_time) {
                        document.getElementById('next-run').textContent = data.next_run_time;
                    }
                } else {
                    statusDiv.innerHTML = '<div class="status error">' + data.message + '</div>';
                }
            })
            .catch(error => {
                document.getElementById('status').innerHTML = '<div class="status error">请求失败: ' + error + '</div>';
            });
        }
    </script>
</body>
</html>
"""

UPDATE_TOKEN_TEMPLATE = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>更新Token</title>
    <style>
        body { font-family: Arial, sans-serif; margin: 20px; background: #f5f5f5; }
        .container { max-width: 600px; margin: 0 auto; background: white; padding: 30px; border-radius: 8px; }
        .form-group { margin-bottom: 20px; }
        label { display: block; margin-bottom: 5px; font-weight: bold; }
        input[type="text"], input[type="password"] { width: 100%; padding: 10px; border: 1px solid #ddd; border-radius: 4px; box-sizing: border-box; }
        .btn { padding: 10px 20px; background: #667eea; color: white; border: none; border-radius: 4px; cursor: pointer; }
        .btn:hover { background: #5568d3; }
        .status { padding: 10px; margin: 10px 0; border-radius: 4px; }
        .status.success { background: #d4edda; color: #155724; }
        .status.error { background: #f8d7da; color: #721c24; }
        .back-link { margin-top: 20px; }
    </style>
</head>
<body>
    <div class="container">
        <h1>更新Token</h1>
        <div id="status"></div>
        <form id="tokenForm" onsubmit="updateToken(event)">
            <div class="form-group">
                <label for="password">密码:</label>
                <input type="password" id="password" name="password" required>
            </div>
            <div class="form-group">
                <label for="token">新的Access Token:</label>
                <input type="text" id="token" name="token" required placeholder="输入新的token">
            </div>
            <button type="submit" class="btn">更新Token</button>
        </form>
        <div class="back-link">
            <a href="/">返回首页</a>
        </div>
    </div>
    
    <script>
        function updateToken(event) {
            event.preventDefault();
            const password = document.getElementById('password').value;
            const token = document.getElementById('token').value;
            
            fetch('/api/update-token', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ password: password, token: token })
            })
            .then(response => response.json())
            .then(data => {
                const statusDiv = document.getElementById('status');
                if (data.success) {
                    statusDiv.innerHTML = '<div class="status success">Token更新成功！</div>';
                    document.getElementById('tokenForm').reset();
                } else {
                    statusDiv.innerHTML = '<div class="status error">' + data.message + '</div>';
                }
            })
            .catch(error => {
                document.getElementById('status').innerHTML = '<div class="status error">请求失败: ' + error + '</div>';
            });
        }
    </script>
</body>
</html>
"""


@app.route('/')
def index():
    """首页：显示最新分析结果"""
    global latest_result
    
    result_html = ""
    if latest_result:
        result_html = generate_html_report(latest_result)
    
    return render_template_string(INDEX_TEMPLATE, result=latest_result, result_html=result_html)


@app.route('/api/result')
def api_result():
    """获取最新结果（JSON格式）"""
    global latest_result
    
    if latest_result is None:
        return jsonify({"success": False, "message": "暂无分析结果"}), 404
    
    return jsonify({
        "success": True,
        "result": latest_result.to_dict()
    })


@app.route('/update-token')
def update_token_page():
    """Token更新页面"""
    return render_template_string(UPDATE_TOKEN_TEMPLATE)


@app.route('/api/update-token', methods=['POST'])
def api_update_token():
    """更新token接口"""
    global config_manager
    
    if config_manager is None:
        try:
            config_manager = ConfigManager()
        except Exception as e:
            return jsonify({"success": False, "message": f"配置管理器初始化失败: {str(e)}"}), 500
    
    data = request.get_json()
    password = data.get("password", "")
    token = data.get("token", "")
    
    # 验证密码
    web_config = config_manager.get_web_config()
    update_password = web_config.get("update_password", "")
    
    if not update_password:
        return jsonify({"success": False, "message": "未配置更新密码，请先设置update_password"}), 400
    
    if password != update_password:
        return jsonify({"success": False, "message": "密码错误"}), 401
    
    # 更新token
    if config_manager.update_token(token):
        return jsonify({"success": True, "message": "Token更新成功"})
    else:
        return jsonify({"success": False, "message": "Token更新失败"}), 500


@app.route('/api/trigger', methods=['POST'])
def api_trigger():
    """手动触发分析"""
    global latest_result
    
    if config_manager is None:
        return jsonify({"success": False, "message": "配置管理器未初始化"}), 500
    
    try:
        result = run_analysis_and_notify(
            config_manager=config_manager,
            send_email=True,  # 手动触发也发送邮件
            save_html=True
        )
        
        if result:
            latest_result = result
            return jsonify({"success": True, "message": "分析完成"})
        else:
            return jsonify({"success": False, "message": "分析失败"}), 500
    except Exception as e:
        return jsonify({"success": False, "message": f"分析出错: {str(e)}"}), 500


@app.route('/history')
def history():
    """历史报告列表页面"""
    report_dir = Path(__file__).parent.parent / "report"
    reports = []
    
    if report_dir.exists():
        # 获取所有HTML报告文件
        html_files = sorted(report_dir.glob("boll_report_*.html"), reverse=True)
        
        for html_file in html_files:
            # 从文件名解析时间信息
            # 文件名格式: boll_report_YYYYMMDD_HHMMSS.html
            filename = html_file.name
            try:
                # 提取时间部分
                time_str = filename.replace("boll_report_", "").replace(".html", "")
                date_part = time_str[:8]  # YYYYMMDD
                time_part = time_str[9:]  # HHMMSS
                
                # 格式化时间显示
                formatted_time = f"{date_part[:4]}-{date_part[4:6]}-{date_part[6:8]} {time_part[:2]}:{time_part[2:4]}:{time_part[4:6]}"
                
                # 获取文件修改时间作为备用
                mtime = datetime.fromtimestamp(html_file.stat().st_mtime)
                
                reports.append({
                    "filename": filename,
                    "name": f"BOLL指标筛选报告 - {formatted_time}",
                    "time": formatted_time,
                    "mtime": mtime
                })
            except Exception:
                # 如果解析失败，使用文件修改时间
                mtime = datetime.fromtimestamp(html_file.stat().st_mtime)
                reports.append({
                    "filename": filename,
                    "name": f"BOLL指标筛选报告 - {mtime.strftime('%Y-%m-%d %H:%M:%S')}",
                    "time": mtime.strftime('%Y-%m-%d %H:%M:%S'),
                    "mtime": mtime
                })
    
    return render_template_string(HISTORY_TEMPLATE, reports=reports)


@app.route('/report/<filename>')
def view_report(filename):
    """查看单个历史报告"""
    report_dir = Path(__file__).parent.parent / "report"
    report_file = report_dir / filename
    
    # 安全检查：确保文件在report目录内，防止路径遍历攻击
    try:
        report_file.resolve().relative_to(report_dir.resolve())
    except ValueError:
        return "无效的报告文件", 404
    
    if not report_file.exists() or not filename.endswith('.html'):
        return "报告文件不存在", 404
    
    # 读取并返回HTML内容
    try:
        with open(report_file, 'r', encoding='utf-8') as f:
            content = f.read()
        return content
    except Exception as e:
        return f"读取报告失败: {str(e)}", 500


def _get_scheduler():
    """获取scheduler实例"""
    try:
        from scheduler import get_scheduler
        return get_scheduler()
    except ImportError:
        return None

@app.route('/schedule')
def schedule_page():
    """定时任务配置页面"""
    global config_manager
    
    if config_manager is None:
        config_manager = ConfigManager()
    
    schedule_config = config_manager.get_schedule_config()
    current_hour = schedule_config.get("hour", 23)
    current_minute = schedule_config.get("minute", 0)
    
    # 获取下次运行时间
    next_run_time = "未知"
    scheduler = _get_scheduler()
    if scheduler:
        next_run = scheduler.get_next_run_time()
        if next_run:
            import pytz
            beijing_tz = pytz.timezone('Asia/Shanghai')
            if next_run.tzinfo:
                next_run_beijing = next_run.astimezone(beijing_tz)
            else:
                next_run_beijing = beijing_tz.localize(next_run)
            next_run_time = next_run_beijing.strftime('%Y-%m-%d %H:%M:%S')
    
    return render_template_string(
        SCHEDULE_TEMPLATE,
        current_hour=current_hour,
        current_minute=current_minute,
        next_run_time=next_run_time
    )


@app.route('/api/schedule', methods=['GET', 'POST'])
def api_schedule():
    """获取或更新定时任务配置"""
    global config_manager
    
    if config_manager is None:
        config_manager = ConfigManager()
    
    if request.method == 'GET':
        # 获取当前配置
        schedule_config = config_manager.get_schedule_config()
        scheduler = _get_scheduler()
        
        next_run_time = None
        if scheduler:
            next_run = scheduler.get_next_run_time()
            if next_run:
                import pytz
                beijing_tz = pytz.timezone('Asia/Shanghai')
                if next_run.tzinfo:
                    next_run_beijing = next_run.astimezone(beijing_tz)
                else:
                    next_run_beijing = beijing_tz.localize(next_run)
                next_run_time = next_run_beijing.strftime('%Y-%m-%d %H:%M:%S')
        
        return jsonify({
            "success": True,
            "config": schedule_config,
            "next_run_time": next_run_time
        })
    
    elif request.method == 'POST':
        # 更新配置
        data = request.get_json()
        hour = data.get("hour")
        minute = data.get("minute")
        
        if hour is None or minute is None:
            return jsonify({"success": False, "message": "缺少必要参数"}), 400
        
        if not (0 <= hour <= 23):
            return jsonify({"success": False, "message": "小时必须在0-23之间"}), 400
        
        if not (0 <= minute <= 59):
            return jsonify({"success": False, "message": "分钟必须在0-59之间"}), 400
        
        # 更新调度器
        scheduler = _get_scheduler()
        if scheduler:
            if scheduler.update_schedule(hour, minute):
                # 获取更新后的下次运行时间
                next_run = scheduler.get_next_run_time()
                next_run_time = None
                if next_run:
                    import pytz
                    beijing_tz = pytz.timezone('Asia/Shanghai')
                    if next_run.tzinfo:
                        next_run_beijing = next_run.astimezone(beijing_tz)
                    else:
                        next_run_beijing = beijing_tz.localize(next_run)
                    next_run_time = next_run_beijing.strftime('%Y-%m-%d %H:%M:%S')
                
                return jsonify({
                    "success": True,
                    "message": "定时任务配置已更新",
                    "next_run_time": next_run_time
                })
            else:
                return jsonify({"success": False, "message": "更新定时任务失败"}), 500
        else:
            return jsonify({"success": False, "message": "调度器未初始化"}), 500


if __name__ == '__main__':
    init_app()
    web_config = config_manager.get_web_config()
    app.run(
        host=web_config.get("host", "0.0.0.0"),
        port=web_config.get("port", 5000),
        debug=False
    )
