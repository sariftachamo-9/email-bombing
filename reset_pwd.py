from app import app, db, User
with app.app_context():
    admin = User.query.filter_by(username='admin').first()
    if admin:
        admin.set_password('admin123')
        db.session.commit()
        print("Admin password reset to 'admin123'")
    else:
        print("Admin user not found")
