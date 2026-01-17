#!/usr/bin/env python3
"""
测试邮件发送功能
"""

import sys
from pathlib import Path

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent))

from config.config_manager import ConfigManager
from notify.email_sender import EmailSender

print("=" * 60)
print("测试SMTP邮件发送功能")
print("=" * 60)

# 加载配置
config_manager = ConfigManager()
email_config = config_manager.get_email_config()

print("\n[1] 检查邮件配置...")
print(f"  SMTP服务器: {email_config.get('smtp_host')}")
print(f"  SMTP端口: {email_config.get('smtp_port')}")
print(f"  SMTP用户: {email_config.get('smtp_user')}")
print(f"  发件人: {email_config.get('from_email')}")
print(f"  收件人: {email_config.get('to_emails')}")

# 检查配置是否完整
if not email_config.get('smtp_host'):
    print("\n✗ 错误: SMTP服务器地址未配置")
    sys.exit(1)

if not email_config.get('smtp_user'):
    print("\n✗ 错误: SMTP用户名未配置")
    sys.exit(1)

if not email_config.get('smtp_password'):
    print("\n✗ 错误: SMTP密码未配置")
    sys.exit(1)

if not email_config.get('from_email'):
    print("\n✗ 错误: 发件人邮箱未配置")
    sys.exit(1)

if not email_config.get('to_emails'):
    print("\n✗ 错误: 收件人邮箱未配置")
    sys.exit(1)

print("\n✓ 邮件配置完整")

# 测试SMTP连接
print("\n[2] 测试SMTP连接...")
try:
    sender = EmailSender(
        smtp_host=email_config.get('smtp_host'),
        smtp_port=email_config.get('smtp_port', 587),
        smtp_user=email_config.get('smtp_user'),
        smtp_password=email_config.get('smtp_password'),
        from_email=email_config.get('from_email')
    )
    
    if sender.test_connection():
        print("✓ SMTP连接测试成功！")
    else:
        print("✗ SMTP连接测试失败")
        sys.exit(1)
        
except Exception as e:
    print(f"✗ SMTP连接测试失败: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)

# 测试发送简单邮件
print("\n[3] 测试发送邮件...")
try:
    # 创建一个简单的测试结果对象
    from watchlist_boll_filter import WatchlistBollFilterResult
    from datetime import datetime
    
    test_result = WatchlistBollFilterResult(
        period=22,
        k=2.0,
        threshold=0.10,
        total_analyzed=0,
        total_found=0,
        update_time=datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    )
    
    success = sender.send_report(
        result=test_result,
        to_emails=email_config.get('to_emails'),
        subject="测试邮件 - BOLL指标筛选系统"
    )
    
    if success:
        print("✓ 测试邮件发送成功！")
        print(f"  请检查收件箱: {', '.join(email_config.get('to_emails'))}")
    else:
        print("✗ 测试邮件发送失败")
        sys.exit(1)
        
except Exception as e:
    print(f"✗ 发送测试邮件失败: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)

print("\n" + "=" * 60)
print("✓ 所有测试通过！邮件功能正常。")
print("=" * 60)
