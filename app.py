import os
import sqlite3
import time
import threading
from datetime import datetime, timedelta
from flask import Flask, render_template, request, redirect, url_for, flash, session, jsonify
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from apscheduler.schedulers.background import BackgroundScheduler
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import logging
import re
import atexit

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Load environment variables
def get_env(key, default=None):
    return os.environ.get(key, default)

app = Flask(__name__)
app.config['SECRET_KEY'] = get_env('SECRET_KEY', os.urandom(24).hex())
app.config['SQLALCHEMY_DATABASE_URI'] = get_env('DATABASE_URL', 'sqlite:///campaigns.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'

# Initialize scheduler
scheduler = BackgroundScheduler()
scheduler.start()

# Register shutdown handler for graceful shutdown
def shutdown_scheduler():
    if scheduler.running:
        scheduler.shutdown(wait=False)
atexit.register(shutdown_scheduler)

# Email validation function
def validate_email(email):
    """Validate email address format"""
    pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    return re.match(pattern, email) is not None

def validate_email_list(email_string):
    """Validate a comma-separated list of email addresses"""
    emails = [e.strip() for e in email_string.split(',')]
    invalid_emails = [e for e in emails if e and not validate_email(e)]
    return len(invalid_emails) == 0, invalid_emails

# Sanitize input function
def sanitize_input(text):
    """Sanitize user input to prevent XSS"""
    if not text:
        return text
    # Proper HTML entity encoding
    text = text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;').replace("'", '&#x27;')
    return text

# Database Models
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    is_admin = db.Column(db.Boolean, default=True)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

class Campaign(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    template_name = db.Column(db.String(100), nullable=False)
    recipient_list = db.Column(db.Text, nullable=False)  # Comma-separated emails
    scheduled_time = db.Column(db.DateTime, nullable=False)
    status = db.Column(db.String(20), default='pending')  # pending, sending, completed, failed
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    sent_count = db.Column(db.Integer, default=0)
    total_recipients = db.Column(db.Integer, default=0)
    bomb_count = db.Column(db.Integer, default=1)

    def __init__(self, **kwargs):
        super(Campaign, self).__init__(**kwargs)

    email_subject = db.Column(db.String(200), nullable=False)
    created_by = db.Column(db.Integer, db.ForeignKey('user.id'))

class EmailLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    campaign_id = db.Column(db.Integer, db.ForeignKey('campaign.id'))
    recipient_email = db.Column(db.String(120))
    sent_at = db.Column(db.DateTime, default=datetime.utcnow)
    status = db.Column(db.String(20))  # success, failed
    error_message = db.Column(db.Text, nullable=True)

class Subscriber(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    name = db.Column(db.String(100), nullable=True)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __init__(self, **kwargs):
        super(Subscriber, self).__init__(**kwargs)

class Setting(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(50), unique=True, nullable=False)
    value = db.Column(db.String(500), nullable=True)

    def __init__(self, **kwargs):
        super(Setting, self).__init__(**kwargs)


# Email configuration (update with your SMTP settings via environment variables)
SMTP_CONFIG = {
    'server': get_env('SMTP_SERVER', 'smtp.gmail.com'),
    'port': int(get_env('SMTP_PORT', '587')),
    'username': get_env('SMTP_USERNAME', ''),
    'password': get_env('SMTP_PASSWORD', ''),
    'use_tls': get_env('SMTP_USE_TLS', 'true').lower() == 'true'
}

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# Create tables and default admin
with app.app_context():
    db.create_all()
    # Create default admin if not exists - use strong password from env or generate random
    if not User.query.filter_by(username='admin').first():
        default_password = get_env('ADMIN_PASSWORD', os.urandom(8).hex())
        admin = User(username='admin', email='admin@example.com', is_admin=True)
        admin.set_password(default_password)
        db.session.add(admin)
        db.session.commit()
        logger.info(f"Default admin created with password: {default_password}")

# Context processor to make settings available to all templates
@app.context_processor
def inject_settings():
    try:
        settings = Setting.query.all()
        config = {s.key: s.value for s in settings}
        return {
            'app_name': config.get('app_name', 'Email Campaign'),
            'default_bomb_count': int(config.get('default_bomb_count', '1')),
            'session_timeout': int(config.get('session_timeout', '30')),
            'login_notifications': config.get('login_notifications', 'false') == 'true'
        }
    except:
        return {
            'app_name': 'Email Campaign',
            'default_bomb_count': 1,
            'session_timeout': 30,
            'login_notifications': False
        }

@app.before_request
def make_session_permanent():
    session.permanent = True
    timeout = 30
    try:
        setting = Setting.query.filter_by(key='session_timeout').first()
        if setting:
            timeout = int(setting.value)
    except:
        pass
    app.permanent_session_lifetime = timedelta(minutes=timeout)


# Helper function to get SMTP settings from DB or fallback to env
def get_smtp_config():
    config = SMTP_CONFIG.copy()
    
    with app.app_context():
        try:
            settings = Setting.query.all()
            db_config = {s.key: s.value for s in settings}
            
            if 'smtp_server' in db_config and db_config['smtp_server']:
                config['server'] = db_config['smtp_server']
            if 'smtp_port' in db_config and db_config['smtp_port']:
                config['port'] = int(db_config['smtp_port'])
            if 'smtp_username' in db_config and db_config['smtp_username']:
                config['username'] = db_config['smtp_username']
            if 'smtp_password' in db_config and db_config['smtp_password']:
                config['password'] = db_config['smtp_password']
            if 'smtp_use_tls' in db_config:
                config['use_tls'] = db_config['smtp_use_tls'].lower() == 'true'
        except Exception as e:
            logger.error(f"Error loading SMTP settings from DB: {e}")
            
    return config

# Helper function to send email
def send_email(recipient, subject, html_content):
    try:
        config = get_smtp_config()
        
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From'] = config['username']
        msg['To'] = recipient

        # Create HTML part
        html_part = MIMEText(html_content, 'html')
        msg.attach(html_part)

        # Send email
        with smtplib.SMTP(config['server'], config['port']) as server:
            if config['use_tls']:
                server.starttls()
            if config['username'] and config['password']:
                server.login(config['username'], config['password'])
            server.send_message(msg)
        
        
        return True, None
    except Exception as e:
        logger.error(f"Failed to send email to {recipient}: {str(e)}")
        return False, str(e)

# Scheduled job function
def send_campaign_emails(campaign_id):
    with app.app_context():
        campaign = Campaign.query.get(campaign_id)
        if not campaign or campaign.status != 'pending':
            return

        campaign.status = 'sending'
        db.session.commit()

        # Get absolute path for template
        base_dir = os.path.dirname(os.path.abspath(__file__))
        template_path = os.path.join(base_dir, 'send_templates', campaign.template_name)
        try:
            with open(template_path, 'r', encoding='utf-8') as f:
                template_content = f.read()
        except FileNotFoundError:
            campaign.status = 'failed'
            db.session.commit()
            logger.error(f"Template not found: {template_path}")
            return

        # Get rate limit delay from settings
        delay = 1.1
        try:
            settings_records = Setting.query.all()
            config = {s.key: s.value for s in settings_records}
            delay = float(config.get('rate_limit_delay', '1.1'))
            app_name = config.get('app_name', 'Email Campaign')
        except:
            app_name = 'Email Campaign'

        # Send to each recipient
        recipients = [email.strip() for email in campaign.recipient_list.split(',')]
        bomb_count = campaign.bomb_count or 1
        success_count = 0

        for recipient in recipients:
            for i in range(bomb_count):
                # Replace placeholders
                email_content = template_content.replace('{{customerName}}', recipient.split('@')[0])
                email_content = email_content.replace('{{orderNumber}}', f'ORD-{campaign.id}-{success_count+1}')
                email_content = email_content.replace('{{orderDate}}', datetime.now().strftime('%Y-%m-%d'))
                email_content = email_content.replace('{{company}}', app_name)
                email_content = email_content.replace('{{year}}', str(datetime.now().year))

                # Send email
                success, error = send_email(recipient, campaign.email_subject, email_content)
                
                # Log the attempt
                log = EmailLog(
                    campaign_id=campaign.id,
                    recipient_email=recipient,
                    status='success' if success else 'failed',
                    error_message=error if not success else None
                )
                db.session.add(log)
                
                if success:
                    success_count += 1
                
                # Dynamic delay to prevent SMTP throttling
                time.sleep(delay)


        # Update campaign status
        campaign.status = 'completed' if success_count > 0 else 'failed'
        campaign.sent_count = success_count
        campaign.total_recipients = len(recipients) * bomb_count
        db.session.commit()

# Routes
@app.route('/')
def index():
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        user = User.query.filter_by(username=username).first()
        if user and user.check_password(password):
            login_user(user)
            
            # Send login notification if enabled
            try:
                setting = Setting.query.filter_by(key='login_notifications').first()
                if setting and setting.value == 'true':
                    app_name_setting = Setting.query.filter_by(key='app_name').first()
                    app_name = app_name_setting.value if app_name_setting else "Email Campaign"
                    subject = f"Security Alert: New Login to {app_name}"
                    html = f"""
                    <h3>Security Alert</h3>
                    <p>A new login was detected for user: <strong>{username}</strong></p>
                    <p>Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
                    <p>If this wasn't you, please secure your account immediately.</p>
                    """
                    send_email(user.email, subject, html)
            except Exception as e:
                logger.error(f"Failed to send login notification: {e}")
                
            return redirect(url_for('admin_dashboard'))
        else:
            flash('Invalid username or password', 'error')
    
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

@app.route('/admin/dashboard')
@login_required
def admin_dashboard():
    campaigns = Campaign.query.order_by(Campaign.created_at.desc()).limit(10).all()
    total_campaigns = Campaign.query.count()
    total_sent = db.session.query(db.func.sum(Campaign.sent_count)).scalar() or 0
    pending_campaigns = Campaign.query.filter_by(status='pending').count()
    
    # Get available templates
    templates = []
    base_dir = os.path.dirname(os.path.abspath(__file__))
    templates_dir = os.path.join(base_dir, 'send_templates')
    if os.path.exists(templates_dir):
        templates = [f for f in os.listdir(templates_dir) if f.endswith('.html')]
        
    # Get available subscribers for the dropdown
    subscribers = Subscriber.query.filter_by(is_active=True).all()
    
    return render_template('admin_dashboard.html', 
                         campaigns=campaigns,
                         total_campaigns=total_campaigns,
                         total_sent=total_sent,
                         pending_campaigns=pending_campaigns,
                         templates=templates,
                         subscribers=subscribers)

@app.route('/admin/campaign/new', methods=['POST'])
@login_required
def new_campaign():
    try:
        name = sanitize_input(request.form.get('campaign_name', ''))
        template = request.form.get('template', '')
        subject = sanitize_input(request.form.get('email_subject', ''))
        recipients = request.form.get('recipients', '')
        scheduled_date = request.form.get('scheduled_date')
        scheduled_time = request.form.get('scheduled_time')
        bomb_count = int(request.form.get('bomb_count', 1))
        
        # Validate email addresses
        is_valid, invalid_emails = validate_email_list(recipients)
        if not is_valid:
            flash(f'Invalid email addresses: {", ".join(invalid_emails)}', 'error')
            return redirect(url_for('admin_dashboard'))
        
        # Validate required fields
        if not name or not template or not subject or not recipients or not scheduled_date or not scheduled_time:
            flash('All fields are required', 'error')
            return redirect(url_for('admin_dashboard'))
        
        # Parse scheduled datetime
        try:
            scheduled_datetime = datetime.strptime(
                f"{scheduled_date} {scheduled_time}", 
                '%Y-%m-%d %H:%M'
            )
        except ValueError:
            flash('Invalid date/time format', 'error')
            return redirect(url_for('admin_dashboard'))
        
        # Create campaign
        campaign = Campaign(
            name=name,
            template_name=template,
            recipient_list=recipients,
            scheduled_time=scheduled_datetime,
            status='pending',
            email_subject=subject,
            created_by=current_user.id,
            bomb_count=bomb_count,
            total_recipients=len([e.strip() for e in recipients.split(',')]) * bomb_count
        )
        
        db.session.add(campaign)
        db.session.commit()
        
        # Schedule the job
        scheduler.add_job(
            func=send_campaign_emails,
            trigger='date',
            run_date=scheduled_datetime,
            args=[campaign.id],
            id=f'campaign_{campaign.id}'
        )
        
        flash('Campaign created and scheduled successfully!', 'success')
    except Exception as e:
        logger.error(f"Error creating campaign: {str(e)}")
        flash(f'Error creating campaign: {str(e)}', 'error')
    
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/campaigns')
@login_required
def campaign_history():
    page = request.args.get('page', 1, type=int)
    campaigns = Campaign.query.order_by(Campaign.created_at.desc()).paginate(page=page, per_page=10)
    return render_template('campaign_history.html', campaigns=campaigns)

@app.route('/admin/campaign/<int:campaign_id>')
@login_required
def campaign_details(campaign_id):
    campaign = Campaign.query.get_or_404(campaign_id)
    logs = EmailLog.query.filter_by(campaign_id=campaign_id).order_by(EmailLog.sent_at.desc()).all()
    return render_template('campaign_details.html', campaign=campaign, logs=logs)

@app.route('/admin/campaign/<int:campaign_id>/cancel', methods=['POST'])
@login_required
def cancel_campaign(campaign_id):
    campaign = Campaign.query.get_or_404(campaign_id)
    if campaign.status == 'pending':
        campaign.status = 'cancelled'
        db.session.commit()
        
        # Remove from scheduler
        try:
            scheduler.remove_job(f'campaign_{campaign_id}')
        except Exception as e:
            logger.warning(f"Could not remove job from scheduler: {e}")
        
        flash('Campaign cancelled successfully', 'success')
    else:
        flash('Cannot cancel campaign that is already sending or completed', 'error')
    
    return redirect(url_for('campaign_history'))

@app.route('/admin/templates')
@login_required
def list_templates():
    templates = []
    base_dir = os.path.dirname(os.path.abspath(__file__))
    templates_dir = os.path.join(base_dir, 'send_templates')
    if os.path.exists(templates_dir):
        for f in os.listdir(templates_dir):
            if f.endswith('.html'):
                with open(os.path.join(templates_dir, f), 'r', encoding='utf-8') as file:
                    content = file.read()
                templates.append({'name': f, 'preview': content[:200] + '...'})
    return render_template('templates.html', templates=templates)

@app.route('/admin/template/upload', methods=['POST'])
@login_required
def upload_template():
    if 'template_file' not in request.files:
        flash('No file uploaded', 'error')
        return redirect(url_for('list_templates'))
    
    file = request.files['template_file']
    if file.filename == '':
        flash('No file selected', 'error')
        return redirect(url_for('list_templates'))
    
    if file and file.filename.endswith('.html'):
        base_dir = os.path.dirname(os.path.abspath(__file__))
        filepath = os.path.join(base_dir, 'send_templates', file.filename)
        file.save(filepath)
        flash('Template uploaded successfully', 'success')
    else:
        flash('Please upload an HTML file', 'error')
    
    return redirect(url_for('list_templates'))

@app.route('/admin/subscribers')
@login_required
def list_subscribers():
    subscribers = Subscriber.query.order_by(Subscriber.created_at.desc()).all()
    return render_template('subscribers.html', subscribers=subscribers)

@app.route('/admin/subscriber/add', methods=['POST'])
@login_required
def add_subscriber():
    email = sanitize_input(request.form.get('email', '')).strip()
    name = sanitize_input(request.form.get('name', '')).strip()
    
    if not email or not validate_email(email):
        flash('Invalid email address', 'error')
        return redirect(url_for('list_subscribers'))
        
    existing = Subscriber.query.filter_by(email=email).first()
    if existing:
        flash('Subscriber already exists', 'error')
        return redirect(url_for('list_subscribers'))
        
    subscriber = Subscriber(email=email, name=name)
    db.session.add(subscriber)
    db.session.commit()
    flash('Subscriber added successfully', 'success')
    return redirect(url_for('list_subscribers'))

@app.route('/admin/subscriber/delete/<int:id>', methods=['POST'])
@login_required
def delete_subscriber(id):
    subscriber = Subscriber.query.get_or_404(id)
    db.session.delete(subscriber)
    db.session.commit()
    flash('Subscriber deleted successfully', 'success')
    return redirect(url_for('list_subscribers'))

@app.route('/admin/settings', methods=['GET', 'POST'])
@login_required
def settings():
    if request.method == 'POST':
        form_type = request.form.get('form_type')
        
        if form_type == 'smtp':
            settings_to_update = {
                'smtp_server': sanitize_input(request.form.get('smtp_server', '')),
                'smtp_port': sanitize_input(request.form.get('smtp_port', '')),
                'smtp_username': sanitize_input(request.form.get('smtp_username', '')),
                'smtp_password': sanitize_input(request.form.get('smtp_password', '')),
                'smtp_use_tls': 'true' if request.form.get('smtp_use_tls') else 'false'
            }
        elif form_type == 'preferences':
            settings_to_update = {
                'app_name': sanitize_input(request.form.get('app_name', 'Email Campaign')),
                'default_bomb_count': sanitize_input(request.form.get('default_bomb_count', '1')),
                'rate_limit_delay': sanitize_input(request.form.get('rate_limit_delay', '1.1'))
            }
        elif form_type == 'password':
            current_pwd = request.form.get('current_password')
            new_pwd = request.form.get('new_password')
            confirm_pwd = request.form.get('confirm_password')
            
            if current_pwd or new_pwd or confirm_pwd or request.form.get('new_username') or request.form.get('new_email'):
                if not current_user.check_password(current_pwd):
                    flash('Current password incorrect', 'error')
                    return redirect(url_for('settings'))
                
                # Update username if provided
                new_username = request.form.get('new_username')
                if new_username and new_username != current_user.username:
                    if User.query.filter_by(username=new_username).first():
                        flash('Username already exists', 'error')
                        return redirect(url_for('settings'))
                    current_user.username = new_username
                
                # Update email if provided
                new_email = request.form.get('new_email')
                if new_email and new_email != current_user.email:
                    if not validate_email(new_email):
                        flash('Invalid email address', 'error')
                        return redirect(url_for('settings'))
                    if User.query.filter_by(email=new_email).first():
                        flash('Email already in use', 'error')
                        return redirect(url_for('settings'))
                    current_user.email = new_email

                # Update password if provided
                if new_pwd:
                    if new_pwd != confirm_pwd:
                        flash('New passwords do not match', 'error')
                        return redirect(url_for('settings'))
                    current_user.set_password(new_pwd)
                
                db.session.commit()
                flash('Account settings updated successfully', 'success')
            
            # Handle other security settings in the same form/tab
            settings_to_update = {
                'session_timeout': sanitize_input(request.form.get('session_timeout', '30')),
                'login_notifications': 'true' if request.form.get('login_notifications') else 'false'
            }
        else:
            flash('Invalid form submission', 'error')
            return redirect(url_for('settings'))
        
        for key, value in settings_to_update.items():
            setting = Setting.query.filter_by(key=key).first()
            if setting:
                setting.value = value
            else:
                db.session.add(Setting(key=key, value=value))
                
        db.session.commit()
        flash('Settings saved successfully', 'success')
        return redirect(url_for('settings'))
        
    # Get all current settings
    settings_items = Setting.query.all()
    current_settings = {s.key: s.value for s in settings_items}
    
    # Defaults for display
    defaults = {
        'smtp_server': SMTP_CONFIG['server'],
        'smtp_port': str(SMTP_CONFIG['port']),
        'smtp_username': SMTP_CONFIG['username'],
        'smtp_password': SMTP_CONFIG['password'],
        'smtp_use_tls': 'true' if SMTP_CONFIG['use_tls'] else 'false',
        'app_name': 'Email Campaign',
        'default_bomb_count': '1',
        'rate_limit_delay': '1.1',
        'session_timeout': '30',
        'login_notifications': 'false'
    }
    
    # Merge current settings into defaults
    for key, val in defaults.items():
        if key not in current_settings:
            current_settings[key] = val
            
    return render_template('settings.html', settings=current_settings)


@app.route('/admin/stats')
@login_required
def campaign_stats():
    # Get statistics for charts
    campaigns_by_status = db.session.query(
        Campaign.status, db.func.count(Campaign.id)
    ).group_by(Campaign.status).all()
    
    daily_sends = db.session.query(
        db.func.date(EmailLog.sent_at), db.func.count(EmailLog.id)
    ).group_by(db.func.date(EmailLog.sent_at)).limit(7).all()
    
    return jsonify({
        'status_counts': dict(campaigns_by_status),
        'daily_sends': dict(daily_sends)
    })

if __name__ == '__main__':
    # Create send_templates directory if it doesn't exist
    base_dir = os.path.dirname(os.path.abspath(__file__))
    templates_dir = os.path.join(base_dir, 'send_templates')
    if not os.path.exists(templates_dir):
        os.makedirs(templates_dir)
    
    # Save your order confirmation template
    order_template = """<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>Order Confirmation #{{orderNumber}}</title>
    <style>
        body { font-family: 'Helvetica Neue', Arial, sans-serif; line-height: 1.6; color: #333; margin: 0; padding: 0; background-color: #f0f0f0; }
        .container { max-width: 600px; margin: 20px auto; background-color: white; border-radius: 8px; overflow: hidden; }
        .header { background-color: #27ae60; color: white; padding: 30px; text-align: center; }
        .checkmark { font-size: 48px; margin-bottom: 10px; }
        .order-info { background-color: #f8f9fa; padding: 20px; border-bottom: 2px solid #27ae60; }
        .content { padding: 30px; }
        .order-details { width: 100%; border-collapse: collapse; margin: 20px 0; }
        .order-details th { background-color: #f8f9fa; padding: 12px; text-align: left; border-bottom: 2px solid #ddd; }
        .order-details td { padding: 12px; border-bottom: 1px solid #ddd; }
        .total-row { font-weight: bold; background-color: #f8f9fa; }
        .shipping-info { margin: 30px 0; padding: 20px; background-color: #f8f9fa; border-radius: 5px; }
        .button { display: inline-block; padding: 12px 30px; background-color: #27ae60; color: white; text-decoration: none; border-radius: 5px; font-weight: bold; }
        .footer { text-align: center; padding: 20px; background-color: #f8f9fa; color: #666; font-size: 14px; }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <div class="checkmark">✓</div>
            <h1>Order Confirmed!</h1>
            <p>Thank you for your purchase</p>
        </div>
        
        <div class="order-info">
            <p><strong>Order Number:</strong> #{{orderNumber}}</p>
            <p><strong>Order Date:</strong> {{orderDate}}</p>
            <p><strong>Payment Method:</strong> Credit Card</p>
        </div>
        
        <div class="content">
            <h2>Hello {{customerName}},</h2>
            <p>Your order has been confirmed and is being processed.</p>
            
            <h3>Order Summary</h3>
            <table class="order-details">
                <thead>
                    <tr>
                        <th>Product</th>
                        <th>Quantity</th>
                        <th>Price</th>
                    </tr>
                </thead>
                <tbody>
                    <tr>
                        <td>Product 1</td>
                        <td>1</td>
                        <td>$99.99</td>
                    </tr>
                    <tr>
                        <td colspan="2" style="text-align: right;"><strong>Subtotal:</strong></td>
                        <td>$99.99</td>
                    </tr>
                    <tr>
                        <td colspan="2" style="text-align: right;"><strong>Shipping:</strong></td>
                        <td>$10.00</td>
                    </tr>
                    <tr class="total-row">
                        <td colspan="2" style="text-align: right;"><strong>Total:</strong></td>
                        <td>$109.99</td>
                    </tr>
                </tbody>
            </table>
            
            <div class="shipping-info">
                <h4>Shipping Address</h4>
                <p>
                    {{customerName}}<br>
                    123 Main Street<br>
                    New York, NY 10001<br>
                    United States
                </p>
            </div>
            
            <div style="text-align: center;">
                <a href="#" class="button">Track Your Order</a>
            </div>
        </div>
        
        <div class="footer">
            <p>Need help? Contact our support team at <a href="mailto:support@{{company}}.com">support@{{company}}.com</a></p>
            <p>© {{year}} {{company}}. All rights reserved.</p>
        </div>
    </div>
</body>
</html>"""
    
    template_path = os.path.join(templates_dir, 'order_confirmation.html')
    if not os.path.exists(template_path):
        with open(template_path, 'w', encoding='utf-8') as f:
            f.write(order_template)
    
    app.run(debug=True, host='0.0.0.0', port=5000)
