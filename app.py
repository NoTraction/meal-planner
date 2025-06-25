from flask import Flask, render_template, request, redirect, url_for, session
import sqlite3
import json
import pyotp
from werkzeug.security import generate_password_hash, check_password_hash
import qrcode
import io
import base64
from datetime import date, datetime, timedelta

app = Flask(__name__, template_folder='templates', static_folder='static')
app.secret_key = 'your-secret-key'

DATABASE = 'MealPlanner.db'

def init_db():
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        cursor.executescript("""
        CREATE TABLE IF NOT EXISTS UserInfo (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            weight REAL DEFAULT NULL,
            height REAL DEFAULT NULL,
            age INTEGER DEFAULT NULL,
            calculated_calories INTEGER DEFAULT NULL,
            target_meals INTEGER DEFAULT 0,
            completed_meals INTEGER DEFAULT 0,
            daily_calories INTEGER DEFAULT 0,
            last_updated TEXT DEFAULT NULL,
            totp_secret TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS DietReq (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            none BOOLEAN DEFAULT 0,
            vegetarian BOOLEAN DEFAULT 0,
            vegan BOOLEAN DEFAULT 0,
            gluten_free BOOLEAN DEFAULT 0,
            dairy_free BOOLEAN DEFAULT 0,
            halal BOOLEAN DEFAULT 0,
            kosher BOOLEAN DEFAULT 0,
            other BOOLEAN DEFAULT 0,
            goal TEXT,
            FOREIGN KEY (user_id) REFERENCES UserInfo(id)
        );

        CREATE TABLE IF NOT EXISTS Meal (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            meal_type TEXT CHECK(meal_type IN ('breakfast', 'lunch', 'dinner')) NOT NULL,
            calories INTEGER NOT NULL,
            ingredients TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS MealPlan (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            day TEXT CHECK(day IN ('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday')) NOT NULL,
            meal_type TEXT CHECK(meal_type IN ('breakfast', 'lunch', 'dinner')) NOT NULL,
            meal_id INTEGER NOT NULL,
            FOREIGN KEY (user_id) REFERENCES UserInfo(id),
            FOREIGN KEY (meal_id) REFERENCES Meal(id)
        );
        """)
        conn.commit()

def calculate_calories(weight, height, age):
    return int(10 * weight + 6.25 * height - 5 * age + 5)

def get_meals(calories):
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        meals = {}
        for meal_type in ['breakfast', 'lunch', 'dinner']:
            cursor.execute("SELECT * FROM Meal WHERE meal_type = ? ORDER BY ABS(calories - ?) LIMIT 1",
                           (meal_type, calories * 0.33))
            result = cursor.fetchone()
            if result:
                meals[meal_type] = {
                    'id': result[0],
                    'name': result[1],
                    'calories': result[3],
                    'ingredients': json.loads(result[4])
                }
    return meals

@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        email = request.form['email'].strip().lower()
        password = request.form['password']

        if not email or not password:
            return render_template('signup.html', error="Email and password are required.")

        hashed_pw = generate_password_hash(password)
        totp_secret = pyotp.random_base32()

        try:
            with sqlite3.connect(DATABASE) as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO UserInfo (email, password, totp_secret) VALUES (?, ?, ?)
                """, (email, hashed_pw, totp_secret))
                conn.commit()
        except sqlite3.IntegrityError:
            return render_template('signup.html', error="Email already registered.")

        session['email'] = email
        session['totp_secret'] = totp_secret
        return redirect(url_for('authenticate'))

    return render_template('signup.html')

@app.route('/authenticate', methods=['GET', 'POST'])
def authenticate():
    if 'email' not in session or 'totp_secret' not in session:
        return redirect(url_for('signup'))

    totp = pyotp.TOTP(session['totp_secret'])
    provisioning_uri = totp.provisioning_uri(session['email'], issuer_name="MealPlannerApp")

    if request.method == 'POST':
        code_entered = request.form['auth_code'].strip()
        if totp.verify(code_entered):
            session['authenticated'] = True
            with sqlite3.connect(DATABASE) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT id, weight, height, age FROM UserInfo WHERE email=?", (session['email'],))
                user = cursor.fetchone()

                if user:
                    user_id, weight, height, age = user
                    session['user_id'] = user_id
                    if weight and height and age:
                        return redirect(url_for('dashboard'))
                    else:
                        return redirect(url_for('profile_setup'))

            return redirect(url_for('signup'))
        else:
            return render_template('authenticate.html', qr_code_img=generate_qr_code_img(provisioning_uri), error="Invalid code.")

    return render_template('authenticate.html', qr_code_img=generate_qr_code_img(provisioning_uri))

def generate_qr_code_img(provisioning_uri):
    qr = qrcode.QRCode(box_size=10, border=2)
    qr.add_data(provisioning_uri)
    qr.make(fit=True)
    img = qr.make_image(fill='black', back_color='white')
    buffered = io.BytesIO()
    img.save(buffered, format="PNG")
    img_str = base64.b64encode(buffered.getvalue()).decode()
    return f'<img src="data:image/png;base64,{img_str}" alt="QR Code">'

@app.route('/profile-setup', methods=['GET', 'POST'])
def profile_setup():
    if not session.get('authenticated'):
        return redirect(url_for('signup'))

    if request.method == 'POST':
        try:
            weight = float(request.form.get('weight'))
            height = float(request.form.get('height'))
            age = int(request.form.get('age'))
            dietary_list = request.form.getlist('dietary')
            goal = request.form.get('goal')
            target_meals = int(request.form.get('target_meals'))
        except (TypeError, ValueError):
            return render_template('profile_setup.html', error="Please enter valid numbers.")

        calories = calculate_calories(weight, height, age)
        user_id = session.get('user_id')
        if not user_id:
            # Fallback: get user_id from email if not in session
            email = session.get('email')
            with sqlite3.connect(DATABASE) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT id FROM UserInfo WHERE email=?", (email,))
                user = cursor.fetchone()
                if not user:
                    return render_template('profile_setup.html', error="User not found. Please sign up again.")
                user_id = user[0]
                session['user_id'] = user_id

        with sqlite3.connect(DATABASE) as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE UserInfo SET weight=?, height=?, age=?, calculated_calories=?, target_meals=?
                WHERE id=?
            """, (weight, height, age, calories, target_meals, user_id))
            dietary_options = ['none', 'vegetarian', 'vegan', 'gluten_free', 'dairy_free', 'halal', 'kosher', 'other']
            dietary_bools = {opt: 1 if opt in dietary_list else 0 for opt in dietary_options}
            cursor.execute("SELECT id FROM DietReq WHERE user_id=?", (user_id,))
            exists = cursor.fetchone()
            if exists:
                cursor.execute(f"""
                    UPDATE DietReq SET
                        {', '.join([f"{opt}=?" for opt in dietary_options])},
                        goal=?
                    WHERE user_id=?
                """, [dietary_bools[opt] for opt in dietary_options] + [goal, user_id])
            else:
                cursor.execute(f"""
                    INSERT INTO DietReq (user_id, {', '.join(dietary_options)}, goal)
                    VALUES (?, {', '.join(['?' for _ in dietary_options])}, ?)
                """, [user_id] + [dietary_bools[opt] for opt in dietary_options] + [goal])
            conn.commit()

        return redirect(url_for('dashboard'))

    return render_template('profile_setup.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form['email'].strip().lower()
        password = request.form['password']
        with sqlite3.connect(DATABASE) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id, password, totp_secret FROM UserInfo WHERE email = ?", (email,))
            user = cursor.fetchone()

        if user and check_password_hash(user[1], password):
            session['user_id'] = user[0]
            session['email'] = email
            session['totp_secret'] = user[2]
            return redirect(url_for('authenticate'))

        return render_template('login.html', error="Invalid email or password.")

    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))

@app.route('/', methods=['GET', 'POST'])
def index():
    if session.get('authenticated') and session.get('user_id'):
        return redirect(url_for('dashboard'))
    return render_template('index.html')

@app.route('/dashboard')
def dashboard():
    if not session.get('authenticated') or not session.get('user_id'):
        return redirect(url_for('signup'))

    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT email, calculated_calories, daily_calories, target_meals, completed_meals FROM UserInfo WHERE id=?", (session['user_id'],))
        user = cursor.fetchone()

    if user and user[1]:
        progresscal = (user[2] / user[1]) * 100
        progresscal = min(progresscal, 100)
        progresscal = round(progresscal, 2)
    else:
        progresscal = 0

    if user and user[3]:
        progressmeals = (user[4] / user[3]) * 100
        progressmeals = min(progressmeals, 100)
        progressmeals = round(progressmeals, 2)
    else:
        progressmeals = 0

    return render_template('dashboard.html', user=user, progresscal=progresscal, progressmeals=progressmeals)

@app.route('/about')
def about():
    return render_template('about.html')

@app.route('/profile', methods=['GET', 'POST'])
def profile():
    if not session.get('authenticated'):
        return redirect(url_for('login'))

    user_id = session.get('user_id')
    user = {}

    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()

        # Handle form submissions
        if request.method == 'POST':
            action = request.form.get('action')
            if action == 'update_personal':
                new_email = request.form.get('email').strip().lower()
                new_password = request.form.get('password')
                if new_password:
                    hashed_pw = generate_password_hash(new_password)
                    cursor.execute("UPDATE UserInfo SET email=?, password=? WHERE id=?",
                                   (new_email, hashed_pw, user_id))
                else:
                    cursor.execute("UPDATE UserInfo SET email=? WHERE id=?", (new_email, user_id))
                conn.commit()
                session['email'] = new_email
            elif action == 'update_goals':
                weight = float(request.form.get('weight'))
                height = float(request.form.get('height'))
                age = int(request.form.get('age'))
                goal = request.form.get('goal')
                target_meals = int(request.form.get('target_meals'))
                # Get dietary requirements as a list of checked values
                dietary_list = request.form.getlist('dietary')
                # Prepare dietary booleans for each option
                dietary_options = ['none', 'vegetarian', 'vegan', 'gluten_free', 'dairy_free', 'halal', 'kosher', 'other']
                dietary_bools = {opt: 1 if opt in dietary_list else 0 for opt in dietary_options}

                calories = calculate_calories(weight, height, age)
                cursor.execute("""
                    UPDATE UserInfo SET weight=?, height=?, age=?, calculated_calories=?, target_meals=?
                    WHERE id=?
                """, (weight, height, age, calories, target_meals, user_id))
                # Upsert into DietReq table
                cursor.execute("SELECT id FROM DietReq WHERE user_id=?", (user_id,))
                exists = cursor.fetchone()
                if exists:
                    cursor.execute(f"""
                        UPDATE DietReq SET
                            {', '.join([f"{opt}=?" for opt in dietary_options])},
                            goal=?
                        WHERE user_id=?
                    """, [dietary_bools[opt] for opt in dietary_options] + [goal, user_id])
                else:
                    cursor.execute(f"""
                        INSERT INTO DietReq (user_id, {', '.join(dietary_options)}, goal)
                        VALUES (?, {', '.join(['?' for _ in dietary_options])}, ?)
                    """, [user_id] + [dietary_bools[opt] for opt in dietary_options] + [goal])
                conn.commit()
            elif action == 'delete_account':
                cursor.execute("DELETE FROM DietReq WHERE user_id=?", (user_id,))
                cursor.execute("DELETE FROM UserInfo WHERE id=?", (user_id,))
                conn.commit()
                session.clear()
                return redirect(url_for('index'))

        # Fetch user info for display
        cursor.execute("""
            SELECT email, weight, height, age, target_meals
            FROM UserInfo WHERE id=?
        """, (user_id,))
        row = cursor.fetchone()
        if row:
            user = {
                'email': row[0],
                'weight': row[1],
                'height': row[2],
                'age': row[3],
                'target_meals': row[4]
            }

        # Fetch dietary requirements and goal from DietReq
        cursor.execute("""
            SELECT none, vegetarian, vegan, gluten_free, dairy_free, halal, kosher, other, goal
            FROM DietReq WHERE user_id=?
        """, (user_id,))
        diet_row = cursor.fetchone()
        if diet_row:
            dietary_options = ['none', 'vegetarian', 'vegan', 'gluten_free', 'dairy_free', 'halal', 'kosher', 'other']
            user['dietary'] = [opt for i, opt in enumerate(dietary_options) if diet_row[i]]
            user['goal'] = diet_row[8]
        else:
            user['dietary'] = []
            user['goal'] = ''

    return render_template('profile.html', user=user)

def complete_meal(user_id, meal_calories):
    conn = sqlite3.connect('your_db.db')
    c = conn.cursor()
    c.execute("SELECT daily_calories, last_updated FROM UserInfo WHERE id = ?", (user_id,))
    result = c.fetchone()
    if result:
        daily_calories, last_updated = result
        today = str(date.today())
        if last_updated != today:
            daily_calories = 0
        updated_calories = daily_calories + meal_calories
        c.execute("""
            UPDATE UserInfo
            SET daily_calories = ?, last_updated = ?
            WHERE id = ?
        """, (updated_calories, today, user_id))
        conn.commit()
    conn.close()

@app.route('/planner/<int:user_id>', methods=['GET', 'POST'])
def planner(user_id):
    days = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT calculated_calories FROM UserInfo WHERE id = ?", (user_id,))
        row = cursor.fetchone()
        if not row:
            return "User not found", 404
        calories = row[0]
        meals = get_meals(calories)

        if request.method == 'POST':
            for day in days:
                for meal_type in ['breakfast', 'lunch', 'dinner']:
                    meal_id = request.form.get(f"{day}_{meal_type}")
                    if meal_id:
                        cursor.execute("INSERT INTO MealPlan (user_id, day, meal_type, meal_id) VALUES (?, ?, ?, ?)",
                                       (user_id, day, meal_type, int(meal_id)))
            conn.commit()
            return redirect(url_for('shopping_list', user_id=user_id))

        meal_options = {}
        for meal_type in ['breakfast', 'lunch', 'dinner']:
            cursor.execute("SELECT id, name FROM Meal WHERE meal_type = ?", (meal_type,))
            meal_options[meal_type] = cursor.fetchall()

    return render_template('mealplanner.html', user_id=user_id, days=days, meal_options=meal_options, default_meals=meals)

@app.route('/shopping')
def shopping():
    if not session.get('authenticated') or not session.get('user_id'):
        return redirect(url_for('login'))

    user_id = session['user_id']
    days = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']

    # Get user's goal from DietReq for default filtering
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT goal FROM DietReq WHERE user_id=?", (user_id,))
        row = cursor.fetchone()
        profile_goal = row[0] if row else "all"

        # Fetch all meals and their tags
        cursor.execute("""
            SELECT id, name, meal_type, calories, ingredients,
                   COALESCE(bulk, 0), COALESCE(cut, 0), COALESCE(health, 0)
            FROM Meal
        """)
        meals = []
        for m in cursor.fetchall():
            meals.append({
                'id': m[0],
                'name': m[1],
                'meal_type': m[2],
                'calories': m[3],
                'ingredients': m[4],
                'bulk': bool(m[5]),
                'cut': bool(m[6]),
                'health': bool(m[7])
            })

    today = datetime.now()
    week_dates = []
    for i in range(14):  # 14 days for two weeks
        day_date = today + timedelta(days=i)
        week_dates.append({
            'label': f"{day_date.strftime('%A')} ({day_date.strftime('%d %m')})",
            'value': day_date.strftime('%Y-%m-%d')
        })

    return render_template(
        'shopping.html',
        meals=meals,
        week_dates=week_dates,
        now=datetime.now(),
        timedelta=timedelta
    )


@app.route('/calendar')
def calendar():
    if not session.get('authenticated') or not session.get('user_id'):
        return redirect(url_for('login'))

    user_id = session['user_id']

    # Get week_start from query param, default to this week's Monday
    week_start_str = request.args.get('week_start')
    if week_start_str:
        week_start = datetime.strptime(week_start_str, '%Y-%m-%d')
    else:
        today = datetime.now()
        week_start = today - timedelta(days=today.weekday())  # Monday

    days = []
    for i in range(7):
        day_date = week_start + timedelta(days=i)
        days.append({
            'name': day_date.strftime('%A'),
            'date': day_date,
            'date_str': day_date.strftime('%d / %m / %y'),      # For the top (week label)
            'short_date': day_date.strftime('%d / %m'),          # For each day
            'date_db': day_date.strftime('%Y-%m-%d')
        })

    week_label = f"{days[0]['date_str']} - {days[-1]['date_str']}"

    meal_types = ['breakfast', 'lunch', 'dinner']
    week_dates = [d['date_db'] for d in days]

    # Fetch planned meals for the user for this week
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT MealPlan.date, MealPlan.meal_type, Meal.name
            FROM MealPlan
            JOIN Meal ON MealPlan.meal_id = Meal.id
            WHERE MealPlan.user_id = ?
              AND MealPlan.date IN ({seq})
        """.format(seq=','.join(['?']*len(week_dates))),
        [user_id] + week_dates)
        planned = cursor.fetchall()

    calendar_data = {}
    for day in days:
        calendar_data[day['date_db']] = {mt: '' for mt in meal_types}
    for date, meal_type, meal_name in planned:
        if date in calendar_data and meal_type in calendar_data[date]:
            calendar_data[date][meal_type] = meal_name

    prev_week = (week_start - timedelta(days=7)).strftime('%Y-%m-%d')
    next_week = (week_start + timedelta(days=7)).strftime('%Y-%m-%d')
    week_label = f"{days[0]['date'].strftime('%d / %m / %y')} - {days[-1]['date'].strftime('%d / %m / %y')}"

    return render_template(
        'calendar.html',
        days=days,
        calendar_data=calendar_data,
        now=datetime.now(),
        prev_week=prev_week,
        next_week=next_week,
        week_label=week_label
    )

@app.route('/add_meal_to_calendar', methods=['POST'])
def add_meal_to_calendar():
    if not session.get('authenticated') or not session.get('user_id'):
        return redirect(url_for('login'))

    user_id = session['user_id']
    meal_id = request.form.get('meal_id')
    meal_type = request.form.get('meal_type')
    date = request.form.get('date')  # This is now a full date string like '2024-06-14'

    if not (meal_id and meal_type and date):
        return redirect(url_for('shopping'))

    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM MealPlan WHERE user_id=? AND date=? AND meal_type=?",
            (user_id, date, meal_type)
        )
        cursor.execute(
            "INSERT INTO MealPlan (user_id, date, meal_type, meal_id) VALUES (?, ?, ?, ?)",
            (user_id, date, meal_type, meal_id)
        )
        conn.commit()

    # Redirect to the calendar week containing the selected date
    from datetime import datetime, timedelta
    selected_date = datetime.strptime(date, '%Y-%m-%d')
    week_start = selected_date - timedelta(days=selected_date.weekday())
    return redirect(url_for('calendar', week_start=week_start.strftime('%Y-%m-%d')))

@app.route('/delete_meal_from_calendar', methods=['POST'])
def delete_meal_from_calendar():
    if not session.get('authenticated') or not session.get('user_id'):
        return redirect(url_for('login'))
    user_id = session['user_id']
    date = request.form.get('date')
    meal_type = request.form.get('meal_type')
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM MealPlan WHERE user_id=? AND date=? AND meal_type=?",
            (user_id, date, meal_type)
        )
        conn.commit()
    return redirect(url_for('calendar'))

@app.route('/complete_meal_from_calendar', methods=['POST'])
def complete_meal_from_calendar():
    if not session.get('authenticated') or not session.get('user_id'):
        return redirect(url_for('login'))
    user_id = session['user_id']
    date = request.form.get('date')
    meal_type = request.form.get('meal_type')
    meal_name = request.form.get('meal_name')

    # Get meal calories
    with sqlite3.connect(DATABASE) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT Meal.id, Meal.calories
            FROM MealPlan
            JOIN Meal ON MealPlan.meal_id = Meal.id
            WHERE MealPlan.user_id=? AND MealPlan.date=? AND MealPlan.meal_type=? AND Meal.name=?
        """, (user_id, date, meal_type, meal_name))
        meal_row = cursor.fetchone()
        if not meal_row:
            return redirect(url_for('calendar'))
        meal_id, meal_calories = meal_row

        # 1. Remove meal from calendar
        cursor.execute(
            "DELETE FROM MealPlan WHERE user_id=? AND date=? AND meal_type=?",
            (user_id, date, meal_type)
        )

        # 2. Update completed_meals
        cursor.execute("SELECT completed_meals FROM UserInfo WHERE id=?", (user_id,))
        completed_meals = cursor.fetchone()[0] or 0
        cursor.execute("UPDATE UserInfo SET completed_meals=? WHERE id=?", (completed_meals + 1, user_id))

        # 3. Update daily_calories and last_updated
        today_str = str(date)
        cursor.execute("SELECT daily_calories, last_updated FROM UserInfo WHERE id=?", (user_id,))
        daily_calories, last_updated = cursor.fetchone()
        if last_updated != today_str:
            daily_calories = 0
        updated_calories = (daily_calories or 0) + meal_calories
        cursor.execute("""
            UPDATE UserInfo
            SET daily_calories=?, last_updated=?
            WHERE id=?
        """, (updated_calories, today_str, user_id))

        conn.commit()

    # Redirect to the same week
    from datetime import datetime, timedelta
    selected_date = datetime.strptime(date, '%Y-%m-%d')
    week_start = selected_date - timedelta(days=selected_date.weekday())
    return redirect(url_for('calendar', week_start=week_start.strftime('%Y-%m-%d')))

if __name__ == '__main__':
    init_db()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
import os