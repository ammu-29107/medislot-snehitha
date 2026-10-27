
CREATE DATABASE IF NOT EXISTS medislot
    CHARACTER SET utf8mb4
    COLLATE utf8mb4_unicode_ci;

USE medislot;

-- Shared identity for doctors and patients.
CREATE TABLE IF NOT EXISTS users (
    user_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    role ENUM('DOCTOR', 'PATIENT') NOT NULL,
    full_name VARCHAR(150) NOT NULL,
    email VARCHAR(254) NOT NULL,
    status ENUM('ACTIVE', 'DISABLED') NOT NULL DEFAULT 'ACTIVE',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,

    PRIMARY KEY (user_id),
    UNIQUE KEY uq_users_email (email),
    KEY idx_users_role_status (role, status)
) ENGINE=InnoDB;

-- Password hashes only; never store plaintext passwords.
CREATE TABLE IF NOT EXISTS user_credentials (
    user_id BIGINT UNSIGNED NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    password_updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,

    PRIMARY KEY (user_id),
    CONSTRAINT fk_credentials_user
        FOREIGN KEY (user_id) REFERENCES users(user_id)
        ON DELETE RESTRICT
) ENGINE=InnoDB;

-- Doctor-specific profile information.
CREATE TABLE IF NOT EXISTS doctor_profiles (
    user_id BIGINT UNSIGNED NOT NULL,
    specialization VARCHAR(150) NOT NULL,
    license_number VARCHAR(100) NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,

    PRIMARY KEY (user_id),
    UNIQUE KEY uq_doctor_license (license_number),
    CONSTRAINT fk_doctor_profile_user
        FOREIGN KEY (user_id) REFERENCES users(user_id)
        ON DELETE RESTRICT
) ENGINE=InnoDB;

-- Patient-specific profile information.
CREATE TABLE IF NOT EXISTS patient_profiles (
    user_id BIGINT UNSIGNED NOT NULL,
    date_of_birth DATE NULL,
    phone_number VARCHAR(20) NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,

    PRIMARY KEY (user_id),
    CONSTRAINT fk_patient_profile_user
        FOREIGN KEY (user_id) REFERENCES users(user_id)
        ON DELETE RESTRICT
) ENGINE=InnoDB;


-- Separate doctor identity and credential records.
CREATE TABLE IF NOT EXISTS doctors (
    doctor_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    full_name VARCHAR(150) NOT NULL,
    email VARCHAR(254) NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    specialization VARCHAR(150) NOT NULL,
    license_number VARCHAR(100) NOT NULL,
    status ENUM('ACTIVE', 'DISABLED') NOT NULL DEFAULT 'ACTIVE',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (doctor_id),
    UNIQUE KEY uq_doctors_email (email),
    UNIQUE KEY uq_doctors_license (license_number)
) ENGINE=InnoDB;

-- Separate patient identity and credential records.
CREATE TABLE IF NOT EXISTS patients (
    patient_id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    full_name VARCHAR(150) NOT NULL,
    email VARCHAR(254) NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    date_of_birth DATE NULL,
    phone_number VARCHAR(20) NULL,
    status ENUM('ACTIVE', 'DISABLED') NOT NULL DEFAULT 'ACTIVE',
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (patient_id),
    UNIQUE KEY uq_patients_email (email)
) ENGINE=InnoDB;

-- Migrate existing doctor accounts without changing their IDs.
INSERT IGNORE INTO doctors (
    doctor_id, full_name, email, password_hash,
    specialization, license_number, status, created_at
)
SELECT
    u.user_id, u.full_name, u.email, c.password_hash,
    d.specialization, d.license_number, u.status, u.created_at
FROM users u
JOIN user_credentials c ON c.user_id = u.user_id
JOIN doctor_profiles d ON d.user_id = u.user_id
WHERE u.role = 'DOCTOR';

-- Migrate existing patient accounts without changing their IDs.
INSERT IGNORE INTO patients (
    patient_id, full_name, email, password_hash,
    date_of_birth, phone_number, status, created_at
)
SELECT
    u.user_id, u.full_name, u.email, c.password_hash,
    p.date_of_birth, p.phone_number, u.status, u.created_at
FROM users u
JOIN user_credentials c ON c.user_id = u.user_id
JOIN patient_profiles p ON p.user_id = u.user_id
WHERE u.role = 'PATIENT';
