from meal_subsidy import create_app

app = create_app()

if __name__ == "__main__":
    # Direct local run: python wsgi.py
    app.run(host="0.0.0.0", port=8000, threaded=True, debug=False)
