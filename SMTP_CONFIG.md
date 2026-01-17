# SMTP邮件配置说明

## 配置文件位置

配置文件位于：`config/config.yaml`

## 需要配置的字段

在 `config/config.yaml` 文件的 `email` 部分，需要填写以下字段：

```yaml
email:
  smtp_host: "smtp.gmail.com"  # SMTP服务器地址
  smtp_port: 587  # SMTP端口
  smtp_user: "your-email@gmail.com"  # SMTP用户名（通常是邮箱地址）
  smtp_password: "your-app-password"  # SMTP密码（建议使用应用专用密码）
  from_email: "your-email@gmail.com"  # 发件人邮箱
  to_emails:  # 收件人邮箱列表
    - "recipient1@example.com"
    - "recipient2@example.com"
```

## 常见邮箱SMTP配置

### Gmail

```yaml
email:
  smtp_host: "smtp.gmail.com"
  smtp_port: 587
  smtp_user: "your-email@gmail.com"
  smtp_password: "your-app-password"  # 需要使用应用专用密码，不是普通密码
  from_email: "your-email@gmail.com"
  to_emails:
    - "recipient@example.com"
```

**重要提示**：Gmail需要使用应用专用密码（App Password），不能使用普通密码。

获取Gmail应用专用密码的步骤：
1. 登录Google账号
2. 进入"管理您的Google账号" → "安全性"
3. 开启"两步验证"（如果还没开启）
4. 在"安全性"页面找到"应用专用密码"
5. 选择"邮件"和"其他（自定义名称）"，生成密码
6. 使用生成的16位密码作为 `smtp_password`

### QQ邮箱

```yaml
email:
  smtp_host: "smtp.qq.com"
  smtp_port: 587
  smtp_user: "your-qq-number@qq.com"
  smtp_password: "your-authorization-code"  # 需要使用授权码，不是QQ密码
  from_email: "your-qq-number@qq.com"
  to_emails:
    - "recipient@example.com"
```

**重要提示**：QQ邮箱需要使用授权码，不是QQ密码。

获取QQ邮箱授权码的步骤：
1. 登录QQ邮箱
2. 进入"设置" → "账户"
3. 找到"POP3/IMAP/SMTP/Exchange/CardDAV/CalDAV服务"
4. 开启"POP3/SMTP服务"或"IMAP/SMTP服务"
5. 点击"生成授权码"，按提示发送短信
6. 使用生成的授权码作为 `smtp_password`

### 163邮箱

```yaml
email:
  smtp_host: "smtp.163.com"
  smtp_port: 587
  smtp_user: "your-email@163.com"
  smtp_password: "your-authorization-code"  # 需要使用授权码
  from_email: "your-email@163.com"
  to_emails:
    - "recipient@example.com"
```

**重要提示**：163邮箱需要使用授权码。

获取163邮箱授权码的步骤：
1. 登录163邮箱
2. 进入"设置" → "POP3/SMTP/IMAP"
3. 开启"POP3/SMTP服务"或"IMAP/SMTP服务"
4. 点击"生成授权码"，按提示操作
5. 使用生成的授权码作为 `smtp_password`

### 企业邮箱（以腾讯企业邮箱为例）

```yaml
email:
  smtp_host: "smtp.exmail.qq.com"
  smtp_port: 587
  smtp_user: "your-email@yourcompany.com"
  smtp_password: "your-password"
  from_email: "your-email@yourcompany.com"
  to_emails:
    - "recipient@example.com"
```

## 配置完成后测试

配置完成后，可以运行以下命令测试邮件发送功能：

```bash
# 激活虚拟环境
source venv/bin/activate

# 运行测试脚本（会尝试发送测试邮件）
python -c "
from config.config_manager import ConfigManager
from notify.email_sender import EmailSender

config_manager = ConfigManager()
email_config = config_manager.get_email_config()

if email_config.get('smtp_host') and email_config.get('smtp_user'):
    sender = EmailSender(
        smtp_host=email_config.get('smtp_host'),
        smtp_port=email_config.get('smtp_port', 587),
        smtp_user=email_config.get('smtp_user'),
        smtp_password=email_config.get('smtp_password'),
        from_email=email_config.get('from_email')
    )
    if sender.test_connection():
        print('✓ SMTP连接测试成功！')
    else:
        print('✗ SMTP连接测试失败，请检查配置')
else:
    print('请先配置SMTP信息')
"
```

## 安全建议

1. **不要将配置文件提交到Git仓库**：确保 `config/config.yaml` 在 `.gitignore` 中
2. **使用应用专用密码**：对于Gmail、QQ邮箱等，使用应用专用密码而不是普通密码
3. **定期更换密码**：建议定期更换SMTP密码
4. **限制访问权限**：确保配置文件只有必要的用户才能访问

## 常见问题

### 1. 连接超时
- 检查服务器防火墙是否允许SMTP端口（通常是587或465）
- 检查SMTP服务器地址是否正确

### 2. 认证失败
- 确认使用的是应用专用密码/授权码，而不是普通密码
- 检查用户名和密码是否正确
- 对于Gmail，确保已开启两步验证

### 3. 邮件发送失败
- 检查收件人邮箱地址是否正确
- 检查发件人邮箱是否被限制发送
- 查看服务器日志获取详细错误信息

## 配置示例

完整的配置示例（Gmail）：

```yaml
email:
  smtp_host: "smtp.gmail.com"
  smtp_port: 587
  smtp_user: "myemail@gmail.com"
  smtp_password: "abcd efgh ijkl mnop"  # Gmail应用专用密码
  from_email: "myemail@gmail.com"
  to_emails:
    - "recipient1@example.com"
    - "recipient2@example.com"
```

配置完成后，保存文件，然后可以运行项目测试邮件发送功能。
