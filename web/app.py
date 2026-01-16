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

# 初始化配置管理器（模块级别）
try:
    config_manager = ConfigManager()
except Exception as e:
    print(f"警告: 配置管理器初始化失败: {e}")


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
            send_email=False,  # 手动触发不自动发邮件
            save_html=True
        )
        
        if result:
            latest_result = result
            return jsonify({"success": True, "message": "分析完成"})
        else:
            return jsonify({"success": False, "message": "分析失败"}), 500
    except Exception as e:
        return jsonify({"success": False, "message": f"分析出错: {str(e)}"}), 500


if __name__ == '__main__':
    init_app()
    web_config = config_manager.get_web_config()
    app.run(
        host=web_config.get("host", "0.0.0.0"),
        port=web_config.get("port", 5000),
        debug=False
    )
