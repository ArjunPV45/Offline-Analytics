

# Multi-Source Zone Visitor Counter - Deployment Script for Raspberry Pi
# This script sets up the environment and deploys the application

set -e  # Exit on any error

echo "=== Multi-Source Zone Visitor Counter Deployment ==="
echo "Deploying on Raspberry Pi with Hailo AI acceleration"

# Configuration
APP_DIR="/opt/visitor-counter"
SERVICE_NAME="visitor-counter"
USER="raspberry5"
PYTHON_ENV="/opt/visitor-counter/venv"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# Check if running as root for system setup
check_root() {
    if [[ $EUID -eq 0 ]]; then
        log_info "Running as root - proceeding with system setup"
    else
        log_error "This script needs to be run as root for system setup"
        echo "Usage: sudo ./deploy.sh"
        exit 1
    fi
}

# Install system dependencies
install_system_deps() {
    log_info "Installing system dependencies..."
    
    apt-get update
    
    # Python development packages
    apt-get install -y python3-dev python3-pip python3-venv
    
    # GStreamer dependencies
    apt-get install -y \
        gstreamer1.0-tools \
        gstreamer1.0-plugins-good \
        gstreamer1.0-plugins-bad \
        gstreamer1.0-plugins-ugly \
        libgstreamer1.0-dev \
        libgstreamer-plugins-base1.0-dev
    
    # Python GI bindings
    apt-get install -y \
        python3-gi \
        python3-gi-cairo \
        gir1.2-gtk-3.0 \
        libgirepository1.0-dev \
        pkg-config
    
    # OpenCV dependencies
    apt-get install -y \
        libopencv-dev \
        python3-opencv
    
    log_info "System dependencies installed successfully"
}

# Create application directory structure
setup_app_directory() {
    log_info "Setting up application directory structure..."
    
    # Create main directory
    mkdir -p $APP_DIR
    cd $APP_DIR
    
    # Create subdirectories
    mkdir -p {logs,data,templates,static,config}
    
    # Set ownership
    chown -R $USER:$USER $APP_DIR
    
    log_info "Directory structure created"
}

# Setup Python virtual environment
setup_python_env() {
    log_info "Setting up Python virtual environment..."
    
    # Create virtual environment as the application user
    sudo -u $USER python3 -m venv $PYTHON_ENV
    
    # Activate and upgrade pip
    sudo -u $USER $PYTHON_ENV/bin/pip install --upgrade pip
    
    log_info "Python virtual environment created"
}

# Install Python dependencies
install_python_deps() {
    log_info "Installing Python dependencies..."
    
    # Install from requirements.txt if it exists
    if [ -f "requirements.txt" ]; then
        sudo -u $USER $PYTHON_ENV/bin/pip install -r requirements.txt
    else
        # Install basic dependencies
        sudo -u $USER $PYTHON_ENV/bin/pip install \
            Flask==2.3.3 \
            Flask-SocketIO==5.3.6 \
            opencv-python==4.8.1.78 \
            numpy==1.24.3 \
            eventlet==0.33.3
    fi
    
    log_info "Python dependencies installed"
}

# Copy application files
deploy_app_files() {
    log_info "Deploying application files..."
    
    # Copy Python modules
    for file in config.py zone_counter.py gstreamer_pipeline.py socketio_handlers.py video_stream.py web_routes.py main.py; do
        if [ -f "$file" ]; then
            cp "$file" $APP_DIR/
            chown $USER:$USER $APP_DIR/$file
        else
            log_warn "File $file not found - skipping"
        fi
    done
    
    # Copy templates if they exist
    if [ -d "templates" ]; then
        cp -r templates/* $APP_DIR/templates/
        chown -R $USER:$USER $APP_DIR/templates/
    fi
    
    # Copy static files if they exist
    if [ -d "static" ]; then
        cp -r static/* $APP_DIR/static/
        chown -R $USER:$USER $APP_DIR/static/
    fi
    
    log_info "Application files deployed"
}

# Create systemd service
create_systemd_service() {
    log_info "Creating systemd service..."
    
    cat > /etc/systemd/system/$SERVICE_NAME.service << EOF
[Unit]
Description=Multi-Source Zone Visitor Counter
After=network.target

[Service]
Type=simple
User=$USER
Group=$USER
WorkingDirectory=$APP_DIR
Environment=PATH=$PYTHON_ENV/bin
ExecStart=$PYTHON_ENV/bin/python main.py
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

    # Reload systemd and enable service
    systemctl daemon-reload
    systemctl enable $SERVICE_NAME
    
    log_info "Systemd service created and enabled"
}

# Setup log rotation
setup_log_rotation() {
    log_info "Setting up log rotation..."
    
    cat > /etc/logrotate.d/$SERVICE_NAME << EOF
$APP_DIR/logs/*.log {
    daily
    missingok
    rotate 7
    compress
    delaycompress
    notifempty
    create 644 $USER $USER
    postrotate
        systemctl reload $SERVICE_NAME
    endscript
}
EOF

    log_info "Log rotation configured"
}

# Setup firewall rules
setup_firewall() {
    log_info "Setting up firewall rules..."
    
    # Check if ufw is installed and active
    if command -v ufw > /dev/null; then
        ufw allow 5000/tcp comment "Visitor Counter Web Interface"
        log_info "Firewall rules added"
    else
        log_warn "UFW not installed - skipping firewall setup"
    fi
}

# Verify Hailo dependencies
verify_hailo_deps() {
    log_info "Verifying Hailo dependencies..."
    
    # Check if Hailo model file exists
    MODEL_PATH="/home/raspberry5/hailo-rpi5-examples/resources/yolov6n.hef"
    if [ -f "$MODEL_PATH" ]; then
        log_info "Hailo model file found at $MODEL_PATH"
    else
        log_warn "Hailo model file not found at $MODEL_PATH"
        log_warn "Please ensure Hailo SDK is properly installed"
    fi
    
    # Test Hailo Python imports (as application user)
    if sudo -u $USER $PYTHON_ENV/bin/python -c "import hailo" 2>/dev/null; then
        log_info "Hailo Python module available"
    else
        log_warn "Hailo Python module not available"
        log_warn "Please install Hailo Python bindings"
    fi
}

# Main deployment function
main() {
    log_info "Starting deployment process..."
    
    check_root
    install_system_deps
    setup_app_directory
    setup_python_env
    install_python_deps
    deploy_app_files
    create_systemd_service
    setup_log_rotation
    setup_firewall
    verify_hailo_deps
    
    log_info "Deployment completed successfully!"
    echo ""
    echo "Next steps:"
    echo "1. Review configuration in $APP_DIR/config.py"
    echo "2. Start the service: sudo systemctl start $SERVICE_NAME"
    echo "3. Check status: sudo systemctl status $SERVICE_NAME"
    echo "4. View logs: sudo journalctl -u $SERVICE_NAME -f"
    echo "5. Access web interface at: http://localhost:5000"
    echo ""
    echo "Service management commands:"
    echo "  Start:   sudo systemctl start $SERVICE_NAME"
    echo "  Stop:    sudo systemctl stop $SERVICE_NAME"
    echo "  Restart: sudo systemctl restart $SERVICE_NAME"
    echo "  Status:  sudo systemctl status $SERVICE_NAME"
}

# Run main function
main "$@"
