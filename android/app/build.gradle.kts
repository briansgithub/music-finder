plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
    id("org.jetbrains.kotlin.plugin.compose")
}
android {
    namespace = "com.brian.musicrec"
    compileSdk = 35
    defaultConfig {
        applicationId = "com.brian.musicrec"
        minSdk = 26          // Pixel-era; covers Pixel 1+
        targetSdk = 35
        versionCode = 4
        versionName = "0.5.0"
        vectorDrawables.useSupportLibrary = true
    }
    signingConfigs {
        getByName("debug") {
            // Default Android debug keystore (~/.android/debug.keystore) used automatically;
            // explicit block keeps Studio/CI happy.
        }
    }
    buildTypes {
        getByName("debug") {
            isMinifyEnabled = false
            signingConfig = signingConfigs.getByName("debug")
            applicationIdSuffix = ".debug"
            versionNameSuffix = "-debug"
        }
        getByName("release") {
            isMinifyEnabled = false
            // Release signing: configure in Android Studio or set MUSICREC_STORE_* env later
            signingConfig = signingConfigs.getByName("debug") // debug-signed until release keystore exists
        }
    }
    buildFeatures { compose = true }
    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions { jvmTarget = "17" }
    packaging { resources.excludes += "/META-INF/{AL2.0,LGPL2.1}" }
}
dependencies {
    implementation(project(":core"))
    implementation(platform("androidx.compose:compose-bom:2024.10.01"))
    implementation("androidx.compose.ui:ui")
    implementation("androidx.compose.material3:material3")
    implementation("androidx.compose.ui:ui-tooling-preview")
    debugImplementation("androidx.compose.ui:ui-tooling")
    implementation("androidx.activity:activity-compose:1.9.3")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.8.7")
    implementation("androidx.lifecycle:lifecycle-viewmodel-compose:2.8.7")
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.9.0")
    implementation("androidx.core:core-ktx:1.13.1")
}
