"""
邮件通知服务
通过SMTP发送HTML格式的邮件报告
"""

import smtplib
import ssl
import sys
from pathlib import Path
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import List, Optional

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent))
from report.html_generator import generate_html_report
from watchlist_boll_filter import WatchlistBollFilterResult


class EmailSender:
    """邮件发送器"""
    
    def __init__(self, smtp_host: str, smtp_port: int, smtp_user: str, 
                 smtp_password: str, from_email: str):
        """
        初始化邮件发送器
        
        Args:
            smtp_host: SMTP服务器地址
            smtp_port: SMTP端口
            smtp_user: SMTP用户名
            smtp_password: SMTP密码
            from_email: 发件人邮箱
        """
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.smtp_user = smtp_user
        self.smtp_password = smtp_password
        self.from_email = from_email
    
    def send_report(self, result: WatchlistBollFilterResult, 
                   to_emails: List[str], subject: Optional[str] = None) -> bool:
        """
        发送HTML格式的报告邮件
        
        Args:
            result: BOLL筛选结果对象
            to_emails: 收件人邮箱列表
            subject: 邮件主题，默认自动生成
        
        Returns:
            是否发送成功
        """
        if not to_emails:
            print("警告: 没有收件人邮箱")
            return False
        
        try:
            # 生成HTML报告
            html_content = generate_html_report(result, "BOLL指标筛选报告")
            
            # 创建邮件
            msg = MIMEMultipart('alternative')
            msg['From'] = self.from_email
            msg['To'] = ', '.join(to_emails)
            msg['Subject'] = subject or f"BOLL指标筛选报告 - {result.update_time}"
            
            # 添加HTML内容
            html_part = MIMEText(html_content, 'html', 'utf-8')
            msg.attach(html_part)
            
            # 发送邮件
            # 根据端口选择使用SSL还是STARTTLS
            if self.smtp_port == 465:
                # 使用SSL
                context = ssl.create_default_context()
                server = smtplib.SMTP_SSL(self.smtp_host, self.smtp_port, context=context)
                try:
                    server.login(self.smtp_user, self.smtp_password)
                    server.sendmail(self.from_email, to_emails, msg.as_string())
                finally:
                    server.quit()
            else:
                # 使用STARTTLS（默认587端口）
                context = ssl.create_default_context()
                server = smtplib.SMTP(self.smtp_host, self.smtp_port)
                try:
                    server.starttls(context=context)  # 启用TLS加密
                    server.login(self.smtp_user, self.smtp_password)
                    server.sendmail(self.from_email, to_emails, msg.as_string())
                finally:
                    server.quit()
            
            print(f"邮件已成功发送到: {', '.join(to_emails)}")
            return True
            
        except smtplib.SMTPAuthenticationError:
            print("错误: SMTP认证失败，请检查用户名和密码")
            return False
        except smtplib.SMTPException as e:
            print(f"错误: SMTP发送失败: {e}")
            import traceback
            traceback.print_exc()
            return False
        except Exception as e:
            print(f"错误: 发送邮件时发生异常: {e}")
            import traceback
            traceback.print_exc()
            return False
    
    def test_connection(self) -> bool:
        """
        测试SMTP连接
        
        Returns:
            连接是否成功
        """
        try:
            # 根据端口选择使用SSL还是STARTTLS
            if self.smtp_port == 465:
                # 使用SSL
                context = ssl.create_default_context()
                server = smtplib.SMTP_SSL(self.smtp_host, self.smtp_port, context=context)
                try:
                    server.login(self.smtp_user, self.smtp_password)
                finally:
                    server.quit()
            else:
                # 使用STARTTLS（默认587端口）
                context = ssl.create_default_context()
                server = smtplib.SMTP(self.smtp_host, self.smtp_port)
                try:
                    server.starttls(context=context)
                    server.login(self.smtp_user, self.smtp_password)
                finally:
                    server.quit()
            print("SMTP连接测试成功")
            return True
        except Exception as e:
            print(f"SMTP连接测试失败: {e}")
            return False
