CREATE TABLE users (
    id SERIAL PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE subjects (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users (id)
);

CREATE TABLE topics (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    subject_id INTEGER NOT NULL,
    FOREIGN KEY (subject_id) REFERENCES subjects (id)
);

CREATE TABLE tasks (
    id SERIAL PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT,
    estimated_minutes INTEGER NOT NULL,
    priority INTEGER NOT NULL DEFAULT 1,
    difficulty TEXT,
    deadline TEXT,
    resource_url TEXT,
    time_studied INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'Incomplete',
    completed_at TEXT,
    topic_id INTEGER,
    FOREIGN KEY (topic_id) REFERENCES topics (id)
);

CREATE TABLE prerequisites (
    task_id INTEGER NOT NULL,
    prerequisite_id INTEGER NOT NULL,
    PRIMARY KEY (task_id, prerequisite_id)
);

CREATE TABLE study_sessions (
    id SERIAL PRIMARY KEY,
    task_id INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    stopped_at TEXT
);
