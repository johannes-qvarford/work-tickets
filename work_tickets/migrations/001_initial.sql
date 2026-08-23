CREATE TABLE IF NOT EXISTS categories (
  id INTEGER NOT NULL PRIMARY KEY,
  name VARCHAR(80) NOT NULL,
  UNIQUE (name)
);

CREATE TABLE IF NOT EXISTS jira_config (
  id INTEGER NOT NULL PRIMARY KEY,
  base_url VARCHAR(300) NOT NULL,
  browser_base_url VARCHAR(300) DEFAULT '' NOT NULL,
  email VARCHAR(320) NOT NULL,
  api_token VARCHAR(300) NOT NULL,
  project_key VARCHAR(40) NOT NULL,
  issue_type VARCHAR(80) NOT NULL,
  completed_statuses VARCHAR(500) NOT NULL,
  updated_at DATETIME NOT NULL
);

CREATE TABLE IF NOT EXISTS tickets (
  id INTEGER NOT NULL PRIMARY KEY,
  parent_id INTEGER,
  summary VARCHAR(240) NOT NULL,
  description TEXT NOT NULL,
  planned_date DATE,
  position INTEGER NOT NULL,
  local_completed BOOLEAN NOT NULL,
  jira_issue_key VARCHAR(40),
  jira_status_name VARCHAR(80),
  synced_at DATETIME,
  created_at DATETIME NOT NULL,
  updated_at DATETIME NOT NULL,
  category_id INTEGER,
  FOREIGN KEY(parent_id) REFERENCES tickets (id),
  FOREIGN KEY(category_id) REFERENCES categories (id)
);